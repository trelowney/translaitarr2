"""Media inspection: ffprobe-based subtitle/audio detection and source selection.

Pure helpers (no DB, no network) shared by the scanner (compute status) and,
later, the translator (extract the chosen track). Mirrors the skip rules of the
original translAItarr: a file is "done" if it already carries Czech as audio, an
embedded subtitle, or a sidecar; otherwise we pick the best source subtitle by
the configured language priority, preferring text over image (PGS/VOBSUB).
"""
import json
import logging
import os
import subprocess

log = logging.getLogger("translaitarr2")

CZECH_CODES = {"cze", "ces", "cs"}
IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle"}

# Normalise the many ISO-639 spellings to the codes used in source_priority.
LANG_ALIASES = {
    "en": "eng", "eng": "eng",
    "fr": "fra", "fre": "fra", "fra": "fra",
    "de": "deu", "ger": "deu", "deu": "deu",
    "es": "spa", "spa": "spa",
    "cs": "cze", "cze": "cze", "ces": "cze",
}


def _norm(lang):
    lang = (lang or "").lower()
    return LANG_ALIASES.get(lang, lang)


def ffprobe_streams(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return []
        return json.loads(out.stdout or "{}").get("streams", [])
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return []


def _lang(stream):
    return _norm(stream.get("tags", {}).get("language", ""))


def _is_forced_or_sdh(stream):
    disp = stream.get("disposition", {})
    if disp.get("forced") or disp.get("hearing_impaired"):
        return True
    title = (stream.get("tags", {}).get("title", "") or "").lower()
    return any(w in title for w in ("forced", "sdh", "hearing", "commentary"))


def has_czech_audio(streams):
    return any(s.get("codec_type") == "audio" and _lang(s) in CZECH_CODES for s in streams)


def has_czech_subtitle(streams):
    """True for an embedded Czech subtitle — text OR image (PGS/VOBSUB)."""
    return any(s.get("codec_type") == "subtitle" and _lang(s) in CZECH_CODES for s in streams)


def has_target_sidecar(path, target_code):
    stem = os.path.splitext(path)[0]
    return os.path.exists(f"{stem}.{target_code}.srt")


def best_source_subtitle(streams, source_priority):
    """Return (kind, index, lang) for the best translatable source subtitle, or
    None. ``kind`` is 'text' or 'image'. Prefers text over image, honours the
    priority order, skips Czech/forced/SDH; non-prioritised languages rank last.
    """
    subs = [s for s in streams if s.get("codec_type") == "subtitle" and not _is_forced_or_sdh(s)]
    prio = [_norm(code) for code in source_priority]

    def rank(s):
        lang = _lang(s)
        return prio.index(lang) if lang in prio else len(prio)

    for want_image in (False, True):  # text first, then image (OCR)
        cands = [
            s for s in subs
            if (s.get("codec_name") in IMAGE_CODECS) == want_image and _lang(s) not in CZECH_CODES
        ]
        cands.sort(key=rank)
        if cands:
            s = cands[0]
            return ("image" if want_image else "text", s.get("index"), _lang(s))
    return None


def classify(path, cfg):
    """Compute subtitle status for one video file.

    Returns a dict: status (display), chip (colour), translatable (bool),
    reason, and for translatable files the chosen source track.
    """
    target = cfg["languages"]["target"]["code"]
    if not os.path.exists(path):
        return {"status": "File not found", "chip": "gray", "translatable": False, "reason": "missing"}
    if has_target_sidecar(path, target):
        return {"status": "Translated", "chip": "blue", "translatable": False, "reason": "sidecar"}

    streams = ffprobe_streams(path)
    if has_czech_audio(streams) or has_czech_subtitle(streams):
        return {"status": "Has Czech", "chip": "green", "translatable": False, "reason": "embedded"}

    src = best_source_subtitle(streams, cfg["languages"]["source_priority"])
    if src is None:
        return {"status": "No source subtitle", "chip": "red", "translatable": False, "reason": "no_source"}

    kind, index, lang = src
    return {
        "status": "Needs translation" + (" (OCR)" if kind == "image" else ""),
        "chip": "amber",
        "translatable": True,
        "reason": "translatable",
        "src_kind": kind,
        "src_index": index,
        "src_lang": lang,
    }
