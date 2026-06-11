"""Translation engine: extract/OCR a source subtitle and translate it to the
target language with Gemini, writing a sidecar ``<video>.<code>.srt``.

Ported from the original translAItarr pipeline and adapted to translAItarr2's
nested config. Track selection and skip rules come from :mod:`media`. The Gemini
call uses ``requests`` (API key in a header, not the URL) and the proven
adaptive-batch loop: shrink the batch on truncation/quality failure, fall back
through the configured model list on rate limits, and never discard entries that
already translated cleanly.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests

import media

log = logging.getLogger("translaitarr2")

# OCR step language keyed by the source stream's ffprobe lang code:
#   [0] tesseract traineddata name (pgsrip --language)
#   [1] alpha-2 code that MUST be in the .sup filename or pgsrip filters it out.
OCR_LANG_MAP = {
    "eng": ("eng", "en"), "en": ("eng", "en"),
    "fra": ("fra", "fr"), "fre": ("fra", "fr"), "fr": ("fra", "fr"),
    "deu": ("deu", "de"), "ger": ("deu", "de"), "de": ("deu", "de"),
    "spa": ("spa", "es"), "es": ("spa", "es"),
}
LANG_NAMES = {
    "eng": "English", "fra": "French", "deu": "German", "spa": "Spanish",
    "pol": "Polish", "ita": "Italian", "por": "Portuguese", "cze": "Czech",
}

DEFAULT_MODELS = ["gemini-2.0-flash", "gemini-2.0-flash-lite"]


class GeminiError(Exception):
    def __init__(self, http_code, message):
        super().__init__(message)
        self.http_code = http_code


class AllModelsExhaustedError(Exception):
    """Every configured model returned 429/5xx — likely all daily limits hit."""


# ── SRT helpers ───────────────────────────────────────────────────────────────

def parse_srt(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    entries = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 2 or not re.match(r"^\d+$", lines[0].strip()) or "-->" not in lines[1]:
            continue
        entries.append((int(lines[0].strip()), lines[1], lines[2:] if len(lines) > 2 else [""]))
    return entries


def count_entries(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if re.match(r"^\d+\s*$", line))
    except OSError:
        return 0


# ── Extraction / OCR ──────────────────────────────────────────────────────────

def extract_subtitle(file_path, stream_idx, out_path):
    log.info("ffmpeg: extracting stream %s from '%s'", stream_idx, Path(file_path).name)
    r = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", file_path,
         "-map", f"0:{stream_idx}", "-c:s", "srt", out_path, "-y"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()}")
    log.info("Extracted %s entries (%s B)", count_entries(out_path), os.path.getsize(out_path))


def ocr_pgs_to_srt(file_path, stream_idx, src_lang_code, out_path, tmp_dir):
    """Extract a PGS (Blu-ray bitmap) subtitle stream and OCR it to text SRT via
    pgsrip + Tesseract, so the rest of the pipeline is unchanged."""
    ocr_lang, fname_lang = OCR_LANG_MAP.get(src_lang_code, ("eng", "en"))
    # pgsrip reads the track language from the filename — the alpha-2 code must be
    # in it (e.g. track.en.sup) or it filters the track out.
    sup_path = os.path.join(tmp_dir, f"track.{fname_lang}.sup")
    log.info("ffmpeg: extracting PGS stream %s -> %s", stream_idx, Path(sup_path).name)
    r = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", file_path,
         "-map", f"0:{stream_idx}", "-c:s", "copy", sup_path, "-y"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg PGS extract failed: {r.stderr.strip()}")

    log.info("OCR: pgsrip + Tesseract (lang=%s)…", ocr_lang)
    r = subprocess.run(
        ["pgsrip", "--language", ocr_lang, "--force", sup_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pgsrip OCR failed: {(r.stderr or r.stdout).strip()}")

    produced = sorted(Path(tmp_dir).glob("track*.srt"))
    if not produced:
        raise RuntimeError("pgsrip produced no SRT output")
    shutil.move(str(produced[0]), out_path)
    n = count_entries(out_path)
    log.info("OCR done: %s entries recognized (%s B)", n, os.path.getsize(out_path))
    if n == 0:
        raise RuntimeError("OCR produced an empty SRT")
    return n


# ── SDH removal ───────────────────────────────────────────────────────────────

def remove_sdh(src_path, dst_path, sdh):
    do_brackets = sdh.get("brackets", True)
    do_parens = sdh.get("parens", True)
    do_music = sdh.get("music", True)
    do_speaker = sdh.get("speaker", True)
    do_uppercase = sdh.get("uppercase", False)

    def clean_block(text_lines):
        block = "\n".join(text_lines)
        if do_brackets:
            block = re.sub(r"\[.*?\]", "", block, flags=re.DOTALL)
            block = re.sub(r"\{.*?\}", "", block, flags=re.DOTALL)
            block = re.sub(r"\[[^\]]*$", "", block, flags=re.MULTILINE)
            block = re.sub(r"^[^\[]*\]", "", block, flags=re.MULTILINE)
        if do_parens:
            block = re.sub(r"\(.*?\)", "", block, flags=re.DOTALL)
            block = re.sub(r"\([^\)]*$", "", block, flags=re.MULTILINE)
            block = re.sub(r"^[^\(]*\)", "", block, flags=re.MULTILINE)
        if do_music:
            block = re.sub(r"^[♪♫\s\-]+$", "", block, flags=re.MULTILINE)
            block = re.sub(r"♪.*?♪", "", block, flags=re.DOTALL)

        out = []
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            if do_music and re.match(r"^[♪♫\s\-]+$", line):
                continue
            if do_speaker:
                line = re.sub(r"^[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ\s\-\'\.]{1,30}:\s*", "", line)
            if do_uppercase and re.match(r"^[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ\s\.\!\?]+$", line) and len(line) > 1:
                continue
            line = re.sub(r"^[\s,\-\.]+|[\s,\-\.]+$", "", line)
            if line:
                out.append(line)
        return out

    kept, removed = [], 0
    for _, tc, text in parse_srt(src_path):
        new_text = clean_block(text)
        if new_text:
            kept.append((tc, new_text))
        else:
            removed += 1

    with open(dst_path, "w", encoding="utf-8") as f:
        for new_num, (tc, text) in enumerate(kept, start=1):
            f.write(f"{new_num}\n{tc}\n" + "\n".join(text) + "\n\n")
    log.info("SDH: removed %s, kept %s", removed, len(kept))
    return removed


def validate_srt(src_path, dst_path, v):
    """Drop cues that fail sanity bounds (text length / duration) — usually OCR
    junk or non-dialogue. Returns how many were dropped."""
    min_c = v.get("min_chars", 1)
    max_c = v.get("max_chars", 200)
    min_d = v.get("min_duration_ms", 100)
    max_d = v.get("max_duration_s", 15) * 1000
    kept, dropped = [], 0
    for _, tc, text in parse_srt(src_path):
        m = re.search(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})", tc)
        dur = (_tc_to_ms(m.group(2)) - _tc_to_ms(m.group(1))) if m else 0
        chars = len(" ".join(text).strip())
        if chars < min_c or chars > max_c or dur < min_d or dur > max_d:
            dropped += 1
            continue
        kept.append((tc, text))
    with open(dst_path, "w", encoding="utf-8") as f:
        for n, (tc, text) in enumerate(kept, start=1):
            f.write(f"{n}\n{tc}\n" + "\n".join(text) + "\n\n")
    log.info("Validation: dropped %s, kept %s", dropped, len(kept))
    return dropped


# ── Gemini ────────────────────────────────────────────────────────────────────

def list_available_models(api_key):
    """Query the Gemini API for models that support generateContent.

    Returns a list of bare model ids (e.g. 'gemini-2.0-flash'). Flash-family
    models are listed first (the ones this app actually uses), then the rest.
    """
    r = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": api_key}, timeout=10,
    )
    r.raise_for_status()
    names = []
    for m in r.json().get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            name = m.get("name", "").split("/")[-1]
            if name:
                names.append(name)
    flash = [n for n in names if "flash" in n]
    rest = [n for n in names if "flash" not in n]
    return flash + rest


def list_openrouter_models(api_key=""):
    """OpenRouter's catalogue (public endpoint, no credit used). Returns
    [{id, name, free}] — free = a $0 (':free') model."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=12)
    r.raise_for_status()
    out = []
    for m in r.json().get("data", []):
        mid = m.get("id", "")
        if not mid:
            continue
        pr = m.get("pricing", {}) or {}
        free = mid.endswith(":free") or (
            str(pr.get("prompt", "")) in ("0", "0.0") and str(pr.get("completion", "")) in ("0", "0.0"))
        out.append({"id": mid, "name": m.get("name", mid), "free": free})
    return out


def openrouter_key_info(api_key):
    """Validate an OpenRouter key; returns its info (limit/usage). Raises on error."""
    r = requests.get("https://openrouter.ai/api/v1/key",
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", {})


def openrouter_credits(api_key):
    """Account credit balance (not the per-key limit, which is usually unset).
    Returns {total, usage}; remaining = total - usage. Raises on error."""
    r = requests.get("https://openrouter.ai/api/v1/credits",
                     headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    r.raise_for_status()
    d = r.json().get("data", {})
    return {"total": float(d.get("total_credits", 0)), "usage": float(d.get("total_usage", 0))}


def normalize_openai_base(url):
    """Be lenient about what the user pastes: accept either the `/v1` base or the
    full `/v1/chat/completions` URL (Lingarr-style), returning the bare base."""
    base = (url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base


def list_openai_models(base_url, api_key=""):
    """Model ids from any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM,
    Groq, DeepSeek…). GET {base_url}/models. Returns a flat list of ids."""
    base = normalize_openai_base(base_url)
    if not base:
        raise ValueError("base URL is required")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = requests.get(f"{base}/models", headers=headers, timeout=12)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") or payload.get("models") or []
    ids = []
    for m in data:
        mid = (m.get("id") or m.get("name")) if isinstance(m, dict) else m
        if mid:
            ids.append(mid)
    return ids


def cloudflare_base(account_id):
    return f"https://api.cloudflare.com/client/v4/accounts/{(account_id or '').strip()}/ai/v1"


def list_cloudflare_models(account_id, api_key):
    """Workers AI text-generation models via Cloudflare's native catalogue
    (GET /accounts/{id}/ai/models/search) — CF's OpenAI `/v1/models` path 405s.
    Returns @cf/ ids."""
    acct = (account_id or "").strip()
    r = requests.get(f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/models/search",
                     headers={"Authorization": f"Bearer {api_key}"},
                     params={"per_page": 200, "task": "Text Generation"}, timeout=12)
    r.raise_for_status()
    out = []
    for m in r.json().get("result", []):
        name = m.get("name")
        task = (m.get("task") or {}).get("name", "")
        if name and task == "Text Generation":
            out.append(name)
    return out


def list_anthropic_models(api_key, base_url=""):
    """Claude models via the native Anthropic Models API (GET /v1/models). Free,
    no tokens spent. Returns a flat list of ids."""
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    r = requests.get(f"{base}/v1/models",
                     headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}, timeout=12)
    r.raise_for_status()
    return [m.get("id") for m in r.json().get("data", []) if m.get("id")]


# ── DeepL (dedicated machine translation, not an LLM) ─────────────────────────
# DeepL uses uppercase codes; the target sometimes needs a regional variant.
DEEPL_TARGET = {
    "cs": "CS", "sk": "SK", "en": "EN-US", "de": "DE", "fr": "FR", "es": "ES",
    "it": "IT", "pt": "PT-PT", "pl": "PL", "nl": "NL", "ru": "RU", "uk": "UK",
    "ja": "JA", "ko": "KO", "zh": "ZH", "ro": "RO", "el": "EL", "tr": "TR",
    "sv": "SV", "da": "DA", "fi": "FI", "no": "NB", "hu": "HU", "bg": "BG",
}
DEEPL_SOURCE = {
    "english": "EN", "czech": "CS", "french": "FR", "german": "DE", "spanish": "ES",
    "italian": "IT", "portuguese": "PT", "polish": "PL", "dutch": "NL", "russian": "RU",
}


def _deepl_base(api_key):
    # Free-tier keys end in ':fx' and use a different host.
    return "https://api-free.deepl.com" if api_key.strip().endswith(":fx") else "https://api.deepl.com"


def deepl_usage(api_key):
    """DeepL character usage/limit for the current period (also validates the key)."""
    r = requests.get(f"{_deepl_base(api_key)}/v2/usage",
                     headers={"Authorization": f"DeepL-Auth-Key {api_key.strip()}"}, timeout=10)
    r.raise_for_status()
    d = r.json()
    return {"count": int(d.get("character_count", 0)), "limit": int(d.get("character_limit", 0))}


def _deepl_translate(texts, target_code, source_name, cfg):
    """Translate a list of strings via DeepL; returns a same-length, same-order list."""
    key = cfg["deepl"]["api_key"].strip()
    if not key:
        raise GeminiError("000", "DeepL API key is not set")
    body = {"text": texts, "target_lang": DEEPL_TARGET.get(target_code, target_code.upper())}
    src = DEEPL_SOURCE.get((source_name or "").lower())
    if src:
        body["source_lang"] = src
    try:
        r = requests.post(f"{_deepl_base(key)}/v2/translate", json=body,
                          timeout=cfg["translation"].get("api_timeout", 1200),
                          headers={"Authorization": f"DeepL-Auth-Key {key}",
                                   "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise GeminiError("000", f"network error: {e}") from e
    if r.status_code != 200:
        try:
            err = r.json().get("message", r.text[:300])
        except ValueError:
            err = r.text[:300]
        raise GeminiError(str(r.status_code), f"HTTP {r.status_code}: {err}")
    return [t.get("text", "") for t in r.json().get("translations", [])]


# ── LibreTranslate (open-source / self-hostable MT) ──────────────────────────
LIBRE_SOURCE = {
    "english": "en", "czech": "cs", "french": "fr", "german": "de", "spanish": "es",
    "italian": "it", "portuguese": "pt", "polish": "pl", "dutch": "nl", "russian": "ru",
}


def libretranslate_languages(base_url, api_key=""):
    """Languages a LibreTranslate server supports (also validates the URL/key)."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base URL is required")
    params = {"api_key": api_key.strip()} if api_key and api_key.strip() else {}
    r = requests.get(f"{base}/languages", params=params, timeout=10)
    r.raise_for_status()
    return [m.get("code") for m in r.json() if isinstance(m, dict) and m.get("code")]


def _libretranslate_translate(texts, target_code, source_name, cfg):
    """Translate a list of strings via LibreTranslate; same-length, same-order list."""
    base = (cfg["libretranslate"].get("base_url") or "").strip().rstrip("/")
    if not base:
        raise GeminiError("000", "LibreTranslate base URL is not set")
    body = {"q": texts, "source": LIBRE_SOURCE.get((source_name or "").lower(), "auto"),
            "target": target_code, "format": "text"}
    key = (cfg["libretranslate"].get("api_key") or "").strip()
    if key:
        body["api_key"] = key
    try:
        r = requests.post(f"{base}/translate", json=body,
                          timeout=cfg["translation"].get("api_timeout", 1200),
                          headers={"Content-Type": "application/json"})
    except requests.RequestException as e:
        raise GeminiError("000", f"network error: {e}") from e
    if r.status_code != 200:
        try:
            err = r.json().get("error", r.text[:300])
        except ValueError:
            err = r.text[:300]
        raise GeminiError(str(r.status_code), f"HTTP {r.status_code}: {err}")
    tr = r.json().get("translatedText", [])
    return [tr] if isinstance(tr, str) else tr


# ── More MT engines (Google, Azure, Yandex, Cloudflare m2m100, keyless GTranslate) ──
def _mt_lang(name):
    """Source language name -> ISO-639-1 code, or None to let the engine auto-detect."""
    return LIBRE_SOURCE.get((name or "").lower())


def _http_err(r):
    try:
        j = r.json()
        e = j.get("error")
        msg = (e.get("message") if isinstance(e, dict) else e) or j.get("message") or r.text[:300]
    except ValueError:
        msg = r.text[:300]
    return f"HTTP {r.status_code}: {msg}"


def _google_translate(texts, target_code, source_name, cfg):
    """Google Cloud Translation v2 (official, API key)."""
    key = (cfg["google"].get("api_key") or "").strip()
    if not key:
        raise GeminiError("000", "Google API key is not set")
    body = {"q": texts, "target": target_code, "format": "text"}
    src = _mt_lang(source_name)
    if src:
        body["source"] = src
    try:
        r = requests.post("https://translation.googleapis.com/language/translate/v2",
                          params={"key": key}, json=body,
                          timeout=cfg["translation"].get("api_timeout", 1200))
    except requests.RequestException as e:
        raise GeminiError("000", f"network error: {e}") from e
    if r.status_code != 200:
        raise GeminiError(str(r.status_code), _http_err(r))
    return [t.get("translatedText", "") for t in r.json().get("data", {}).get("translations", [])]


def _azure_translate(texts, target_code, source_name, cfg):
    """Microsoft / Azure Translator (key + resource region)."""
    key = (cfg["azure"].get("api_key") or "").strip()
    if not key:
        raise GeminiError("000", "Azure Translator key is not set")
    params = {"api-version": "3.0", "to": target_code}
    src = _mt_lang(source_name)
    if src:
        params["from"] = src
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"}
    region = (cfg["azure"].get("region") or "").strip()
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    try:
        r = requests.post("https://api.cognitive.microsofttranslator.com/translate",
                          params=params, json=[{"Text": t} for t in texts], headers=headers,
                          timeout=cfg["translation"].get("api_timeout", 1200))
    except requests.RequestException as e:
        raise GeminiError("000", f"network error: {e}") from e
    if r.status_code != 200:
        raise GeminiError(str(r.status_code), _http_err(r))
    return ["".join(x.get("text", "") for x in item.get("translations", [])) for item in r.json()]


def _yandex_translate(texts, target_code, source_name, cfg):
    """Yandex Translate (API key + cloud folder id)."""
    key = (cfg["yandex"].get("api_key") or "").strip()
    if not key:
        raise GeminiError("000", "Yandex API key is not set")
    body = {"texts": texts, "targetLanguageCode": target_code}
    folder = (cfg["yandex"].get("folder_id") or "").strip()
    if folder:
        body["folderId"] = folder
    src = _mt_lang(source_name)
    if src:
        body["sourceLanguageCode"] = src
    try:
        r = requests.post("https://translate.api.cloud.yandex.net/translate/v2/translate",
                          json=body, headers={"Authorization": f"Api-Key {key}",
                                              "Content-Type": "application/json"},
                          timeout=cfg["translation"].get("api_timeout", 1200))
    except requests.RequestException as e:
        raise GeminiError("000", f"network error: {e}") from e
    if r.status_code != 200:
        raise GeminiError(str(r.status_code), _http_err(r))
    return [t.get("text", "") for t in r.json().get("translations", [])]


def _cf_m2m100_translate(texts, target_code, source_name, cfg):
    """Cloudflare Workers AI m2m100 — reuses the 'cloudflare' block's credentials.
    Takes one text per call (no batch array), so it issues one request per cue."""
    acct = (cfg["cloudflare"].get("account_id") or "").strip()
    key = (cfg["cloudflare"].get("api_key") or "").strip()
    if not (acct and key):
        raise GeminiError("000", "Set your Cloudflare account ID + token in the Cloudflare tab first")
    src = _mt_lang(source_name) or "en"
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/@cf/meta/m2m100-1.2b"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    out = []
    for t in texts:
        try:
            r = requests.post(url, json={"text": t, "source_lang": src, "target_lang": target_code},
                              headers=headers, timeout=cfg["translation"].get("api_timeout", 1200))
        except requests.RequestException as e:
            raise GeminiError("000", f"network error: {e}") from e
        if r.status_code != 200:
            raise GeminiError(str(r.status_code), _http_err(r))
        out.append(r.json().get("result", {}).get("translated_text", ""))
    return out


def _gtranslate_free(texts, target_code, source_name, cfg):
    """Unofficial keyless Google endpoint (no auth). Low quality and may rate-limit —
    a best-effort 'works without signing up' option. One request per cue."""
    src = _mt_lang(source_name) or "auto"
    out = []
    for t in texts:
        try:
            r = requests.get("https://translate.googleapis.com/translate_a/single",
                             params={"client": "gtx", "sl": src, "tl": target_code, "dt": "t", "q": t},
                             timeout=cfg["translation"].get("api_timeout", 1200))
        except requests.RequestException as e:
            raise GeminiError("000", f"network error: {e}") from e
        if r.status_code != 200:
            raise GeminiError(str(r.status_code), _http_err(r))
        seg = r.json()[0] or []
        out.append("".join(s[0] for s in seg if s and s[0]))
    return out


# Per-engine batch size + translate fn for the MT path.
_MT_ENGINES = {
    "deepl": (50, _deepl_translate),
    "libretranslate": (25, _libretranslate_translate),
    "google": (100, _google_translate),
    "azure": (50, _azure_translate),
    "yandex": (50, _yandex_translate),
    "cf_m2m100": (50, _cf_m2m100_translate),
    "gtranslate_free": (20, _gtranslate_free),
}


def mt_probe(provider, cfg):
    """Translate a one-word probe ('OK') to validate an MT engine's credentials.
    Returns the translated string."""
    _, fn = _MT_ENGINES[provider]
    out = fn(["OK"], cfg["languages"]["target"]["code"], "English", cfg)
    return out[0] if out else ""


def mt_translate_chunk(provider, srt_path, src_lang, tgt_lang, cfg):
    """Translate an SRT chunk with a dedicated MT engine (DeepL, LibreTranslate…).
    Parses the cues, sends their text as batches, and rebuilds an SRT with the same
    numbers/timecodes — so _translate_all's count-matching accept logic is unchanged."""
    batch, translate_fn = _MT_ENGINES[provider]
    entries = parse_srt(srt_path)
    texts = ["\n".join(t) for _, _, t in entries]
    target_code = cfg["languages"]["target"]["code"]
    log.info("%s -> %s entries (target %s)", provider, len(entries), target_code)
    out = []
    for i in range(0, len(texts), batch):
        out.extend(translate_fn(texts[i:i + batch], target_code, src_lang, cfg))
    if len(out) != len(entries):
        raise GeminiError("000", f"{provider} returned {len(out)} of {len(entries)} translations")
    parts = [f"{num}\n{tc}\n{(tr or '').strip()}\n" for (num, tc, _), tr in zip(entries, out)]
    return "\n".join(parts), "stop"


def _translation_prompt(srt_content, src_lang, tgt_lang):
    return (
        f"Translate subtitles from {src_lang} to {tgt_lang}.\n"
        "Use natural, conversational language as spoken in movies and TV shows.\n"
        "Preserve character personality, emotions, and informal speech.\n"
        "Adapt idioms and slang naturally for the target language.\n"
        "Use informal address between characters by default; switch to formal only "
        "when clearly required by social context, hierarchy, or narrative intent.\n\n"
        "CRITICAL FORMATTING RULES - never violate:\n"
        "- Return ONLY raw SRT. No explanation, no markdown, no code fences.\n"
        "- NEVER skip, merge, split or reorder any subtitle entry.\n"
        "- Keep every NUMBER line unchanged.\n"
        "- Keep every TIMECODE line unchanged, character for character.\n"
        "- Keep every blank separator line unchanged.\n"
        "- Translate ONLY the text/dialogue lines.\n"
        "- Output must have EXACTLY the same number of entries as input.\n\n"
        f"SRT to translate:\n\n{srt_content}"
    )


def _complete(provider, model, prompt, cfg, max_tokens=None):
    """Raw text generation for a provider. Returns (text, finish_reason). Raises
    GeminiError (the fallback reacts to its http_code)."""
    if provider in MT_PROVIDERS:
        raise GeminiError("000", f"{provider} is a translation engine, not a text model")
    timeout = cfg["translation"].get("api_timeout", 1200)
    if max_tokens is None:
        max_tokens = cfg["translation"].get("max_output_tokens", 65536)

    if provider in ("openrouter", "openai_compat", "cloudflare"):
        # All speak the OpenAI /chat/completions shape; only base_url + headers differ.
        if provider == "openrouter":
            base = "https://openrouter.ai/api/v1"
            key = cfg["openrouter"]["api_key"]
            extra = {"HTTP-Referer": "https://github.com/trelowney/translaitarr2",
                     "X-Title": "translAItarr2"}
        elif provider == "cloudflare":
            acct = (cfg["cloudflare"].get("account_id") or "").strip()
            if not acct:
                raise GeminiError("000", "Cloudflare account ID is not set")
            base = cloudflare_base(acct)
            key = cfg["cloudflare"].get("api_key", "")
            extra = {}
        else:
            base = normalize_openai_base(cfg["openai_compat"].get("base_url"))
            key = cfg["openai_compat"].get("api_key", "")
            extra = {}
            if not base:
                raise GeminiError("000", "OpenAI-compatible base URL is not set")
        # Unlike Gemini (where maxOutputTokens is separate from the context window),
        # many OpenAI-style endpoints share one context budget for input+output, so
        # asking for the full 65536-token output overflows the window. Cap the output
        # to comfortably hold a subtitle batch; truncation is handled by the
        # MAX_TOKENS retry path, the same as Gemini.
        max_tokens = min(max_tokens, 16384)
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.25, "max_tokens": max_tokens}
        headers = {"Content-Type": "application/json", **extra}
        if key:  # local servers (Ollama/LM Studio) usually need no auth
            headers["Authorization"] = f"Bearer {key}"
        try:
            r = requests.post(f"{base}/chat/completions", json=body, timeout=timeout, headers=headers)
        except requests.RequestException as e:
            raise GeminiError("000", f"network error: {e}") from e
        if r.status_code != 200:
            try:
                err = r.json().get("error", {}).get("message", r.text[:300])
            except ValueError:
                err = r.text[:300]
            raise GeminiError(str(r.status_code), f"HTTP {r.status_code}: {err}")
        ch = (r.json().get("choices") or [{}])[0]
        finish = "MAX_TOKENS" if ch.get("finish_reason") == "length" else ch.get("finish_reason", "?")
        return (ch.get("message") or {}).get("content", ""), finish

    if provider == "anthropic":
        # Native Anthropic Messages API — its own request shape, not OpenAI.
        base = (cfg["anthropic"].get("base_url") or "https://api.anthropic.com").rstrip("/")
        key = cfg["anthropic"].get("api_key", "")
        max_tokens = min(max_tokens, 16384)  # also keeps non-streaming under the timeout
        # No temperature: Opus 4.7/4.8 reject sampling params; the default is fine for subtitles.
        body = {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
        try:
            r = requests.post(f"{base}/v1/messages", json=body, timeout=timeout, headers=headers)
        except requests.RequestException as e:
            raise GeminiError("000", f"network error: {e}") from e
        if r.status_code != 200:
            try:
                err = r.json().get("error", {}).get("message", r.text[:300])
            except ValueError:
                err = r.text[:300]
            raise GeminiError(str(r.status_code), f"HTTP {r.status_code}: {err}")
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        finish = "MAX_TOKENS" if data.get("stop_reason") == "max_tokens" else data.get("stop_reason", "?")
        return text, finish

    # gemini (default)
    key = cfg["gemini"]["api_key"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.25, "maxOutputTokens": max_tokens}}
    try:
        r = requests.post(url, json=body, timeout=timeout,
                          headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise GeminiError("000", f"network error: {e}") from e
    if r.status_code != 200:
        try:
            err = r.json().get("error", {}).get("message", r.text[:300])
        except ValueError:
            err = r.text[:300]
        raise GeminiError(str(r.status_code), f"HTTP {r.status_code}: {err}")
    cand = (r.json().get("candidates") or [{}])[0]
    finish = cand.get("finishReason", "?")
    return (cand.get("content", {}).get("parts") or [{}])[0].get("text", ""), finish


def call_model_once(provider, model, srt_path, src_lang, tgt_lang, cfg):
    """Translate one SRT chunk with provider+model. Returns (clean_srt, finish)."""
    with open(srt_path, encoding="utf-8") as f:
        srt_content = f.read()
    prompt = _translation_prompt(srt_content, src_lang, tgt_lang)
    log.info("%s -> %s: %s entries", provider, model, count_entries(srt_path))
    t0 = time.time()
    text, finish = _complete(provider, model, prompt, cfg)
    log.info("%s <- finish=%s in %.1fs", provider, finish, time.time() - t0)
    if finish == "MAX_TOKENS":
        log.warning("MAX_TOKENS — output truncated, will retry missing entries")
    if not text:
        raise GeminiError("000", "empty response")
    clean = re.sub(r"^```[^\n]*\n?|```$", "", text, flags=re.MULTILINE)
    m = re.search(r"^\d+\r?\n\d{2}:\d{2}:\d{2},\d{3}", clean, re.MULTILINE)
    if m:
        clean = clean[m.start():]
    return clean, finish


PROVIDERS = ("gemini", "openrouter", "openai_compat", "anthropic", "cloudflare",
             "deepl", "libretranslate", "google", "azure", "yandex",
             "cf_m2m100", "gtranslate_free")
# Dedicated machine-translation engines (not LLMs): translate text segments, no
# prompt/model list. Routed through mt_translate_chunk instead of _complete.
MT_PROVIDERS = ("deepl", "libretranslate", "google", "azure", "yandex",
                "cf_m2m100", "gtranslate_free")
RETRIABLE = ("500", "502", "503", "504", "429", "000", "401", "402")


def provider_configured(cfg, p):
    """True if provider p has enough credentials/config to actually translate."""
    g = cfg.get(p, {})
    if p == "gtranslate_free":
        return True
    if p == "cf_m2m100":
        return bool(cfg["cloudflare"].get("account_id") and cfg["cloudflare"].get("api_key"))
    if p in ("deepl", "google", "azure", "yandex"):
        return bool(g.get("api_key"))
    if p == "libretranslate":
        return bool(g.get("base_url"))
    if p == "openai_compat":
        return bool(g.get("base_url") and g.get("models"))
    if p == "cloudflare":
        return bool(g.get("account_id") and g.get("api_key") and g.get("models"))
    # gemini, openrouter, anthropic — need an API key and at least one model
    return bool(g.get("api_key") and g.get("models"))


def configured_providers(cfg):
    """Providers that are ready to translate (for the per-job picker)."""
    return [p for p in PROVIDERS if provider_configured(cfg, p)]


def provider_chain(cfg):
    """Ordered [(provider, models, default_limit, per_model_limits)] from the ai
    priority slots. Falls back to gemini if nothing is configured (back-compat)."""
    ai = cfg.get("ai", {})
    chain, seen = [], set()
    for slot in ("primary", "secondary", "tertiary"):
        p = ai.get(slot, "none")
        if p in seen:
            continue
        if p in MT_PROVIDERS:  # MT engines have no model list — configured by key/URL
            if p == "cf_m2m100":  # reuses the cloudflare block's credentials
                configured = cfg["cloudflare"].get("account_id") and cfg["cloudflare"].get("api_key")
            elif p == "gtranslate_free":  # keyless — always available once selected
                configured = True
            else:
                configured = cfg.get(p, {}).get("api_key") or cfg.get(p, {}).get("base_url")
            if configured:
                chain.append((p, [p], 10 ** 9, {}))  # char-limited, not request-limited
                seen.add(p)
        elif p in PROVIDERS and cfg.get(p, {}).get("models"):
            chain.append((p, cfg[p]["models"], cfg["limits"].get("max_daily_per_model", 18),
                          cfg[p].get("model_daily_limit", {})))
            seen.add(p)
    if not chain and cfg.get("gemini", {}).get("models"):
        chain.append(("gemini", cfg["gemini"]["models"], cfg["limits"].get("max_daily_per_model", 18),
                      cfg["gemini"].get("model_daily_limit", {})))
    return chain


def call_with_fallback(srt_path, src_lang, tgt_lang, cfg, usage, fails):
    """Try providers in priority order, each model in turn; skip models at their
    daily limit, fall back on 5xx/429/auth errors. Mutates usage and fails."""
    chain = provider_chain(cfg)
    first = chain[0][1][0] if chain and chain[0][1] else None
    last_err = None
    for provider, models, default_limit, per_model in chain:
        for model in models:
            limit = per_model.get(model, default_limit)
            if usage.get(model, 0) >= limit:
                log.info("Model %s at daily limit (%s) — skipping", model, limit)
                continue
            try:
                if provider in MT_PROVIDERS:
                    text, finish = mt_translate_chunk(provider, srt_path, src_lang, tgt_lang, cfg)
                else:
                    text, finish = call_model_once(provider, model, srt_path, src_lang, tgt_lang, cfg)
                usage[model] = usage.get(model, 0) + 1
                if model != first:
                    log.info("Fallback to %s/%s", provider, model)
                return text, model, finish
            except GeminiError as e:
                last_err = e
                if e.http_code in RETRIABLE:
                    fails[model] = fails.get(model, 0) + 1
                    log.warning("%s/%s unavailable (%s), trying next", provider, model, e.http_code)
                    continue
                raise
    raise AllModelsExhaustedError(f"All providers/models unavailable. Last error: {last_err}")


# ── Adaptive batch translation ────────────────────────────────────────────────

def _batch_cap(provider, model, cfg):
    overrides = cfg.get(provider, {}).get("model_batch", {})
    return overrides.get(model) or cfg["translation"].get("batch_size", 150)


def _quality_ok(batch, returned):
    """False when >40% of long (>15 char) entries came back identical to source."""
    long_total = sum(1 for _, _, t in batch if len(" ".join(t)) > 15)
    if long_total == 0:
        return True
    long_same = sum(
        1 for (_, _, s_txt), (_, _, r_txt) in zip(batch, returned)
        if len(" ".join(s_txt)) > 15
        and " ".join(s_txt).strip().lower() == " ".join(r_txt).strip().lower()
    )
    if long_same / long_total > 0.4:
        log.warning("Quality check: %.0f%% of long entries untranslated — smaller batch",
                    100 * long_same / long_total)
        return False
    return True


def _translate_all(srt_path, src_lang, tgt_lang, cfg, tmp, usage, fails):
    """Translate in adaptive chunks. Returns (trl_map, last_model, src_entries, model_calls)."""
    src_entries = parse_srt(srt_path)
    trl_map, model_calls = {}, {}
    used_model = None
    remaining = list(src_entries)
    chain = provider_chain(cfg)
    if chain and chain[0][1]:
        batch_cap = _batch_cap(chain[0][0], chain[0][1][0], cfg)
        preferred = chain[0][1][0]
    else:
        preferred, batch_cap = None, cfg["translation"].get("batch_size", 150)
    log.info("Batch cap: %s (preferred: %s)", batch_cap, preferred)
    send_count = min(len(remaining), batch_cap)
    max_attempts = max(30, len(src_entries) // 50 * 2)
    attempt = 0

    while remaining:
        attempt += 1
        if attempt > max_attempts:
            log.warning("%s entries untranslated after %s attempts, giving up", len(remaining), max_attempts)
            break

        batch = remaining[:send_count]
        chunk_path = os.path.join(tmp, f"attempt_{attempt}.srt")
        with open(chunk_path, "w", encoding="utf-8") as f:
            for num, tc, text in batch:
                f.write(f"{num}\n{tc}\n" + "\n".join(text) + "\n\n")
        if attempt > 1:
            log.info("Retry %s: %s entries", attempt, len(batch))

        try:
            translated_text, model, finish = call_with_fallback(chunk_path, src_lang, tgt_lang, cfg, usage, fails)
        except (GeminiError, AllModelsExhaustedError) as e:
            log.warning("Attempt %s: all models failed (%s) — halving batch", attempt, e)
            send_count = max(50, len(batch) // 2)
            time.sleep(35)  # respect free-tier RPM cooldown
            continue
        used_model = model
        model_calls[model] = model_calls.get(model, 0) + 1

        trl_path = os.path.join(tmp, f"trl_{attempt}.srt")
        with open(trl_path, "w", encoding="utf-8") as f:
            f.write(translated_text)

        prev_remaining = len(remaining)
        returned = parse_srt(trl_path)
        accepted = False

        if len(returned) == len(batch):
            if _quality_ok(batch, returned):
                for (src_num, _, _), (_, _, r_text) in zip(batch, returned):
                    if r_text:
                        trl_map[src_num] = r_text
                accepted = True
        elif finish == "MAX_TOKENS":
            log.info("MAX_TOKENS: %s/%s entries via number matching", len(returned), len(batch))
            for r_num, _, r_text in returned:
                if r_text:
                    trl_map[r_num] = r_text
            accepted = True
        else:
            log.warning("Attempt %s: count mismatch (sent %s, got %s) finish=%s — discarding",
                        attempt, len(batch), len(returned), finish)

        remaining = [(num, tc, text) for num, tc, text in remaining if num not in trl_map]
        newly_done = prev_remaining - len(remaining)
        if remaining:
            if not accepted or newly_done < len(batch) * 0.8:
                send_count = max(50, min(len(batch) // 2, batch_cap))
            else:
                send_count = min(len(remaining), batch_cap)
            log.info("After attempt %s: %s entries still missing", attempt, len(remaining))

    return trl_map, used_model, src_entries, model_calls


# ── Credit line ───────────────────────────────────────────────────────────────

def _tc_to_ms(tc):
    h, m, s_ms = tc.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def _ms_to_tc(ms):
    h = ms // 3600000; ms %= 3600000
    m = ms // 60000; ms %= 60000
    s = ms // 1000; ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def prepend_credit(srt_path, model):
    with open(srt_path, encoding="utf-8") as f:
        content = f.read()
    tcs = re.findall(r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})", content, re.MULTILINE)
    if not tcs:
        return
    pairs = [(_tc_to_ms(s), _tc_to_ms(e)) for s, e in tcs]
    slot = None
    first_start = pairs[0][0]
    if first_start >= 2500:
        cs, ce = max(200, first_start - 4000), first_start - 300
        if ce - cs >= 1500:
            slot = (cs, ce)
    if slot is None:
        for i in range(len(pairs) - 1):
            gs, ge = pairs[i][1], pairs[i + 1][0]
            if gs > 600000:
                break
            if ge - gs >= 3000:
                cs = gs + 200
                ce = min(ge - 300, cs + 4000)
                if ce - cs >= 1500:
                    slot = (cs, ce)
                    break
    if slot is None:
        return
    credit = (f"0\n{_ms_to_tc(slot[0])} --> {_ms_to_tc(slot[1])}\n"
              f"Translated by translAItarr2\n{model}\n\n")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write(credit + content)


# ── Main entry point ──────────────────────────────────────────────────────────

def translate_file(file_path, cfg, force=False, usage=None, fails=None):
    """Translate one video file's best source subtitle to the target language.

    Returns (outcome, model_calls). outcome is 'translated' or 'skipped:<reason>'.
    Raises RuntimeError on unrecoverable problems (no source, extraction failure).
    """
    usage = usage if usage is not None else {}
    fails = fails if fails is not None else {}
    target_code = cfg["languages"]["target"]["code"]
    target_lang = cfg["languages"]["target"]["name"]

    log.info("=" * 60)
    log.info("Processing: %s%s", file_path, " [FORCE]" if force else "")

    if force:
        # Re-translate regardless of skip rules; pick the best source directly.
        src = media.select_source(file_path, cfg)
        if src is None:
            raise RuntimeError("No source subtitle to translate from")
        src_kind, stream_idx, src_lang_code, src_path = src["kind"], src["index"], src["lang"], src["path"]
    else:
        info = media.classify(file_path, cfg)
        if not info["translatable"]:
            log.info("Skip: %s", info["reason"])
            return f"skipped:{info['reason']}", {}
        src_kind, stream_idx, src_lang_code, src_path = (
            info["src_kind"], info["src_index"], info["src_lang"], info.get("src_path"))

    ocr_needed = src_kind == "image"
    src_lang = LANG_NAMES.get(src_lang_code, src_lang_code.upper())
    where = " [PGS -> OCR]" if ocr_needed else (" [sidecar .srt]" if src_kind == "sidecar" else "")
    log.info("Source: %s, lang=%s (%s)%s",
             "external sidecar" if src_kind == "sidecar" else f"stream {stream_idx}",
             src_lang, src_lang_code, where)

    out_srt = Path(file_path).with_suffix("")  # strip extension
    out_srt = out_srt.parent / f"{Path(file_path).stem}.{target_code}.srt"

    with tempfile.TemporaryDirectory(prefix="translaitarr2_") as tmp:
        raw_srt = os.path.join(tmp, "raw.srt")
        if src_kind == "sidecar":
            shutil.copy2(src_path, raw_srt)
            log.info("Source sidecar: %s (%s entries)", os.path.basename(src_path), count_entries(raw_srt))
        elif ocr_needed:
            ocr_pgs_to_srt(file_path, stream_idx, src_lang_code, raw_srt, tmp)
        else:
            extract_subtitle(file_path, stream_idx, raw_srt)

        sdh = cfg.get("sdh", {})
        if any(sdh.get(k) for k in ("brackets", "parens", "music", "speaker", "uppercase")):
            clean_srt = os.path.join(tmp, "clean.srt")
            remove_sdh(raw_srt, clean_srt, sdh)
            srt_to_send = clean_srt
        else:
            srt_to_send = raw_srt

        val = cfg.get("validation", {})
        if val.get("enabled"):
            valid_srt = os.path.join(tmp, "valid.srt")
            validate_srt(srt_to_send, valid_srt, val)
            srt_to_send = valid_srt

        trl_map, used_model, src_entries, model_calls = _translate_all(
            srt_to_send, src_lang, target_lang, cfg, tmp, usage, fails)

        merged_srt = os.path.join(tmp, "merged.srt")
        missing = 0
        with open(merged_srt, "w", encoding="utf-8") as f:
            for num, tc, src_text in src_entries:
                text = trl_map.get(num)
                if not text:
                    text, missing = src_text, missing + 1
                f.write(f"{num}\n{tc}\n" + "\n".join(text) + "\n\n")
        total = len(src_entries)
        log.info("Merge: %s/%s fell back to source", missing, total)

        # Credit line is always added: "Translated by translAItarr2" + the model.
        prepend_credit(merged_srt, used_model or "AI")

        shutil.copy2(merged_srt, str(out_srt))

    log.info("Saved: %s (%s B)", out_srt, os.path.getsize(str(out_srt)))
    log.info("Done: %s/%s fell back, model=%s", missing, total, used_model)
    log.info("=" * 60)
    return "translated", model_calls


# ── Translation verification (semantic, model-judged) ────────────────────────

def _start_ms(tc):
    m = re.search(r"(\d{2}:\d{2}:\d{2},\d{3})", tc)
    return _tc_to_ms(m.group(1)) if m else -1


def _judge(prompt, cfg, usage):
    """One small completion over the provider chain (honouring daily limits) for the
    verification QA pass. Returns (text, model_calls)."""
    last = None
    for provider, models, default_limit, per_model in provider_chain(cfg):
        if provider in MT_PROVIDERS:  # MT engines can't act as an LLM judge
            continue
        for model in models:
            if usage.get(model, 0) >= per_model.get(model, default_limit):
                continue
            try:
                text, _ = _complete(provider, model, prompt, cfg, max_tokens=2048)
            except GeminiError as e:
                last = e
                if e.http_code in RETRIABLE:
                    continue
                raise
            usage[model] = usage.get(model, 0) + 1
            return text, {model: 1}
    raise AllModelsExhaustedError(f"verify: no model available ({last})")


def verify_translation(video_path, cfg, usage=None):
    """Check a finished translation by having the model judge meaning — it tolerates
    paraphrase (a round-trip is never word-for-word), flagging only genuinely wrong,
    untranslated or garbled lines. Returns (result, model_calls). result keys: ok,
    checked, leftover (untranslated source left in), bad (mistranslated samples),
    samples, note."""
    import media
    usage = usage if usage is not None else {}
    target_code = cfg["languages"]["target"]["code"]
    target_lang = cfg["languages"]["target"]["name"]

    cs_path = media.target_sidecar_path(video_path, target_code)
    if not os.path.exists(cs_path):
        return {"ok": False, "note": "no translation to verify"}, {}
    src = media.select_source(video_path, cfg)
    if src is None:
        return {"ok": False, "note": "no source to compare against"}, {}
    src_lang = LANG_NAMES.get(src["lang"], src["lang"].upper())

    with tempfile.TemporaryDirectory(prefix="verify_") as tmp:
        src_srt = os.path.join(tmp, "src.srt")
        if src["kind"] == "sidecar":
            shutil.copy2(src["path"], src_srt)
        elif src["kind"] == "image":
            ocr_pgs_to_srt(video_path, src["index"], src["lang"], src_srt, tmp)
        else:
            extract_subtitle(video_path, src["index"], src_srt)

        src_map = {_start_ms(tc): " ".join(t).strip() for _, tc, t in parse_srt(src_srt)}
        aligned = []
        for _, tc, t in parse_srt(cs_path):
            en = src_map.get(_start_ms(tc))
            if en:
                aligned.append((en, " ".join(t).strip()))
        if not aligned:
            return {"ok": False, "note": "could not align subtitles by timecode"}, {}

        # Lines whose "translation" is identical to the source are prime suspects for
        # untranslated text — but also legitimately identical proper names ("Karl
        # Allen Gibbs"), so don't count them outright: feed them to the judge first
        # and let it decide. The rest of the sample is spread across the file.
        identical = [a for a in aligned if len(a[0]) > 15 and a[1].lower() == a[0].lower()]
        long_pairs = [a for a in aligned if len(a[0]) > 15]
        n = min(int(cfg["translation"].get("verify_samples", 8)), len(long_pairs))
        sample = list(identical[:n])
        rest = [a for a in long_pairs if a not in identical]
        need = n - len(sample)
        if need > 0 and rest:
            sample += [rest[i * len(rest) // need] for i in range(need)]

        model_calls, bad = {}, 0
        if sample:
            lines = "\n".join(f"{i}. SOURCE: {en}\n   TRANSLATION: {cs}"
                              for i, (en, cs) in enumerate(sample, 1))
            prompt = (
                f"You are a subtitle-translation QA reviewer. Each numbered pair has a "
                f"{src_lang} SOURCE line and its {target_lang} TRANSLATION.\n"
                f"Mark a translation BAD only if it is wrong in meaning, left untranslated "
                f"(still {src_lang}), empty, or garbled. Natural paraphrase, different word "
                f"order and style are GOOD — never flag those.\n"
                f"Reply with ONLY a JSON array of the numbers of the BAD ones, e.g. [2,5]. "
                f"If all are fine, reply [].\n\n{lines}"
            )
            try:
                text, model_calls = _judge(prompt, cfg, usage)
            except (GeminiError, AllModelsExhaustedError) as e:
                return {"ok": False, "checked": len(aligned),
                        "note": f"verification call failed: {e}"}, model_calls
            m = re.search(r"\[[\d,\s]*\]", text)
            try:
                bad = len(set(json.loads(m.group(0)))) if m else 0
            except ValueError:
                bad = 0

        ok = bad == 0
        log.info("Verify: %s aligned · %s identical-to-source · %s flagged of %s sampled -> %s",
                 len(aligned), len(identical), bad, len(sample), "OK" if ok else f"{bad} ISSUE(S)")
        return {"ok": ok, "checked": len(aligned), "identical": len(identical),
                "bad": bad, "samples": len(sample)}, model_calls
