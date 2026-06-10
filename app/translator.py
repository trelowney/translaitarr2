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


def call_gemini_once(srt_path, src_lang, tgt_lang, model, cfg):
    """Single Gemini call. Returns (clean_srt_text, finish_reason). Raises GeminiError."""
    api_key = cfg["gemini"]["api_key"]
    timeout = cfg["translation"].get("api_timeout", 1200)
    max_tokens = cfg["translation"].get("max_output_tokens", 65536)

    with open(srt_path, encoding="utf-8") as f:
        srt_content = f.read()

    prompt = (
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
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.25, "maxOutputTokens": max_tokens},
    }

    log.info("Gemini -> %s: %s entries", model, count_entries(srt_path))
    t0 = time.time()
    try:
        r = requests.post(
            url, json=body, timeout=timeout,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        )
    except requests.RequestException as e:
        raise GeminiError("000", f"network error: {e}") from e
    log.info("Gemini <- HTTP %s in %.1fs", r.status_code, time.time() - t0)

    if r.status_code != 200:
        try:
            err = r.json().get("error", {}).get("message", r.text[:300])
        except ValueError:
            err = r.text[:300]
        raise GeminiError(str(r.status_code), f"HTTP {r.status_code}: {err}")

    data = r.json()
    cand = (data.get("candidates") or [{}])[0]
    finish = cand.get("finishReason", "?")
    usage = data.get("usageMetadata", {})
    log.info("Tokens: in=%s out=%s finish=%s",
             usage.get("promptTokenCount", "?"), usage.get("candidatesTokenCount", "?"), finish)
    if finish == "MAX_TOKENS":
        log.warning("MAX_TOKENS — output truncated, will retry missing entries")

    translated = (cand.get("content", {}).get("parts") or [{}])[0].get("text", "")
    if not translated:
        raise GeminiError("000", "empty response from Gemini")

    clean = re.sub(r"^```[^\n]*\n?|```$", "", translated, flags=re.MULTILINE)
    srt_start = re.search(r"^\d+\r?\n\d{2}:\d{2}:\d{2},\d{3}", clean, re.MULTILINE)
    if srt_start:
        clean = clean[srt_start.start():]
    return clean, finish


def call_gemini_with_fallback(srt_path, src_lang, tgt_lang, cfg, usage):
    """Try each configured model in order; skip any that has reached its daily
    limit, fall back on 5xx/429/000. Mutates ``usage`` (model -> calls today)."""
    order = cfg["gemini"].get("models") or DEFAULT_MODELS
    default_limit = cfg["limits"].get("max_daily_per_model", 18)
    per_model = cfg["gemini"].get("model_daily_limit", {})
    last_err = None
    for model in order:
        limit = per_model.get(model, default_limit)
        if usage.get(model, 0) >= limit:
            log.info("Model %s at daily limit (%s) — skipping", model, limit)
            continue
        try:
            text, finish = call_gemini_once(srt_path, src_lang, tgt_lang, model, cfg)
            usage[model] = usage.get(model, 0) + 1
            if model != order[0]:
                log.info("Fallback model used: %s", model)
            return text, model, finish
        except GeminiError as e:
            last_err = e
            if e.http_code in ("500", "502", "503", "504", "429", "000"):
                log.warning("Model %s unavailable (%s), trying next", model, e.http_code)
                continue
            raise
    raise AllModelsExhaustedError(f"All models unavailable/at limit. Last error: {last_err}")


# ── Adaptive batch translation ────────────────────────────────────────────────

def _batch_cap(model, cfg):
    overrides = cfg["gemini"].get("model_batch", {})
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


def _translate_all(srt_path, src_lang, tgt_lang, cfg, tmp, usage):
    """Translate in adaptive chunks. Returns (trl_map, last_model, src_entries, model_calls)."""
    src_entries = parse_srt(srt_path)
    trl_map, model_calls = {}, {}
    used_model = None
    remaining = list(src_entries)
    preferred = (cfg["gemini"].get("models") or DEFAULT_MODELS)[0]
    batch_cap = _batch_cap(preferred, cfg)
    log.info("Batch cap: %s (preferred model: %s)", batch_cap, preferred)
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
            translated_text, model, finish = call_gemini_with_fallback(chunk_path, src_lang, tgt_lang, cfg, usage)
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

def translate_file(file_path, cfg, force=False, usage=None):
    """Translate one video file's best source subtitle to the target language.

    Returns (outcome, model_calls). outcome is 'translated' or 'skipped:<reason>'.
    Raises RuntimeError on unrecoverable problems (no source, extraction failure).
    """
    usage = usage if usage is not None else {}
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
            srt_to_send, src_lang, target_lang, cfg, tmp, usage)

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
