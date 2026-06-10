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

IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle"}

# Normalise the many ISO-639 spellings (2-letter, 639-2/B, 639-2/T) to one
# canonical code per language, so target/source comparisons are robust.
LANG_ALIASES = {
    "en": "eng", "eng": "eng",
    "cs": "cze", "cze": "cze", "ces": "cze",
    "sk": "slk", "slk": "slk", "slo": "slk",
    "de": "deu", "deu": "deu", "ger": "deu",
    "fr": "fra", "fra": "fra", "fre": "fra",
    "es": "spa", "spa": "spa",
    "it": "ita", "ita": "ita",
    "pt": "por", "por": "por",
    "pl": "pol", "pol": "pol",
    "nl": "nld", "nld": "nld", "dut": "nld",
    "ru": "rus", "rus": "rus",
    "uk": "ukr", "ukr": "ukr",
    "hu": "hun", "hun": "hun",
    "ro": "ron", "ron": "ron", "rum": "ron",
    "hr": "hrv", "hrv": "hrv",
    "sr": "srp", "srp": "srp",
    "bg": "bul", "bul": "bul",
    "el": "ell", "ell": "ell", "gre": "ell",
    "tr": "tur", "tur": "tur",
    "sv": "swe", "swe": "swe",
    "da": "dan", "dan": "dan",
    "fi": "fin", "fin": "fin",
    "no": "nor", "nor": "nor",
    "ja": "jpn", "jpn": "jpn",
    "ko": "kor", "kor": "kor",
    "zh": "zho", "zho": "zho", "chi": "zho",
    "ar": "ara", "ara": "ara",
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


def _is_forced(stream):
    title = (stream.get("tags", {}).get("title", "") or "").lower()
    return bool(stream.get("disposition", {}).get("forced")) or "forced" in title


def _is_sdh(stream):
    title = (stream.get("tags", {}).get("title", "") or "").lower()
    return (bool(stream.get("disposition", {}).get("hearing_impaired"))
            or any(w in title for w in ("sdh", "hearing", "commentary")))


def has_target_audio(streams, target_code):
    t = _norm(target_code)
    return any(s.get("codec_type") == "audio" and _lang(s) == t for s in streams)


def has_target_subtitle(streams, target_code):
    """True for an embedded target-language subtitle — text OR image (PGS/VOBSUB)."""
    t = _norm(target_code)
    return any(s.get("codec_type") == "subtitle" and _lang(s) == t for s in streams)


def target_sidecar_path(path, target_code):
    return f"{os.path.splitext(path)[0]}.{target_code}.srt"


def has_target_sidecar(path, target_code):
    return os.path.exists(target_sidecar_path(path, target_code))


def best_source_subtitle(streams, source_priority, target_code):
    """Return (kind, index, lang) for the best translatable source subtitle, or
    None. ``kind`` is 'text' or 'image'. Prefers text over image, honours the
    priority order, excludes the target language, deprioritises forced/SDH;
    non-prioritised languages rank last.
    """
    target = _norm(target_code)
    subs = [s for s in streams
            if s.get("codec_type") == "subtitle" and _lang(s) != target]
    if not subs:
        return None
    prio = [_norm(code) for code in source_priority]

    def rank(s):
        lang = _lang(s)
        lang_score = prio.index(lang) if lang in prio else len(prio)
        is_image = 1 if s.get("codec_name") in IMAGE_CODECS else 0
        # Prefer text over image, then language priority, then non-forced, non-SDH.
        # Forced/SDH are deprioritised, not excluded — a deaf/forced track is still
        # a usable source if it's the only one (SDH artefacts get stripped later).
        return (is_image, lang_score, _is_forced(s), _is_sdh(s), s.get("index", 0))

    best = min(subs, key=rank)
    kind = "image" if best.get("codec_name") in IMAGE_CODECS else "text"
    return (kind, best.get("index"), _lang(best))


def _code_variants(canon):
    """All spelling variants (2-letter, 639-2/B, 639-2/T) for a canonical code."""
    return [k for k, v in LANG_ALIASES.items() if v == canon] or [canon]


def find_source_sidecar(path, source_priority, target_code):
    """Look for an external source-language sidecar next to the video, e.g.
    'Movie.en.srt' / 'Movie.eng.srt', in source-priority order. Returns
    (srt_path, canonical_lang) or None. The target language is excluded; a bare
    'Movie.srt' with no language tag is ignored (ambiguous)."""
    stem = os.path.splitext(path)[0]
    target = _norm(target_code)
    for code in source_priority:
        canon = _norm(code)
        if canon == target:
            continue
        for variant in _code_variants(canon):
            cand = f"{stem}.{variant}.srt"
            if os.path.exists(cand):
                return cand, canon
    return None


def select_source(path, cfg, streams=None):
    """Pick the best source for translation, honouring source_preference:
    'sidecar' = prefer an external .srt then fall back to the video; 'video' =
    only the video's embedded/PGS subtitles. Returns a dict
    {kind: 'sidecar'|'text'|'image', index, lang, path} or None."""
    target = cfg["languages"]["target"]["code"]
    source_priority = cfg["languages"]["source_priority"]
    if cfg["translation"].get("source_preference", "video") == "sidecar":
        sc = find_source_sidecar(path, source_priority, target)
        if sc:
            return {"kind": "sidecar", "index": None, "lang": sc[1], "path": sc[0]}
    if streams is None:
        streams = ffprobe_streams(path)
    vid = best_source_subtitle(streams, source_priority, target)
    if vid:
        kind, index, lang = vid
        return {"kind": kind, "index": index, "lang": lang, "path": None}
    return None


def classify(path, cfg):
    """Compute subtitle status for one video file.

    Returns a dict: status (display), chip (colour), translatable (bool),
    reason, and for translatable files the chosen source track.
    """
    target = cfg["languages"]["target"]["code"]
    target_name = cfg["languages"]["target"].get("name") or target.upper()
    if not os.path.exists(path):
        return {"status": "File not found", "chip": "gray", "translatable": False, "reason": "missing"}

    # A fresh sidecar = done. If the video is newer than our sidecar, the release
    # was upgraded and the old translation is stale → re-translate.
    sidecar = target_sidecar_path(path, target)
    sidecar_stale = False
    if os.path.exists(sidecar):
        try:
            if os.path.getmtime(sidecar) >= os.path.getmtime(path):
                return {"status": "Translated", "chip": "blue", "translatable": False, "reason": "sidecar"}
            sidecar_stale = True
        except OSError:
            return {"status": "Translated", "chip": "blue", "translatable": False, "reason": "sidecar"}

    streams = ffprobe_streams(path)
    has_aud = has_target_audio(streams, target)
    has_sub = has_target_subtitle(streams, target)
    if has_aud or has_sub:
        kinds = " + ".join(p for p, ok in (("audio", has_aud), ("subtitles", has_sub)) if ok)
        return {"status": f"Has {target_name} {kinds}", "chip": "green", "translatable": False, "reason": "embedded"}

    src = select_source(path, cfg, streams)
    if src is None:
        return {"status": "No source subtitle", "chip": "red", "translatable": False, "reason": "no_source"}

    kind = src["kind"]
    if sidecar_stale:
        status = "Needs re-translation (upgrade)"
    else:
        extra = " (OCR)" if kind == "image" else (" (sidecar)" if kind == "sidecar" else "")
        status = "Needs translation" + extra
    return {
        "status": status,
        "chip": "amber",
        "translatable": True,
        "reason": "upgrade" if sidecar_stale else "translatable",
        "src_kind": kind,
        "src_index": src["index"],
        "src_lang": src["lang"],
        "src_path": src["path"],
    }
