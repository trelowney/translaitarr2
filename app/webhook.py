"""Optional Sonarr/Radarr "On Import" webhook.

Without it, new files are found by the automation scan (every N minutes). With it,
Sonarr/Radarr POST their Webhook connection payload here right after importing, and
the file is checked and queued straight away. Only the "Download" (import/upgrade)
event does anything; "Test" just answers OK so the *arr Test button goes green.
"""
import logging
import os
import threading

import db
import media
import scanner

log = logging.getLogger("translaitarr2")


def _episode_label(series, episodes):
    label = series.get("title") or "?"
    if len(episodes) == 1:
        ep = episodes[0]
        se, en = ep.get("seasonNumber"), ep.get("episodeNumber")
        if isinstance(se, int) and isinstance(en, int):
            label += f" S{se:02d}E{en:02d}"
        if ep.get("title"):
            label += f" — {ep['title']}"
    return label


def files_from_payload(p):
    """Return [(arr_path, title)] for an import payload; [] for anything else."""
    if p.get("eventType") != "Download":
        return []
    out = []
    if "series" in p:
        series = p.get("series") or {}
        files = p.get("episodeFiles") or ([p["episodeFile"]] if p.get("episodeFile") else [])
        label = _episode_label(series, p.get("episodes") or [])
        for f in files:
            path = f.get("path") or (os.path.join(series["path"], f["relativePath"])
                                     if series.get("path") and f.get("relativePath") else None)
            if path:
                out.append((path, label if len(files) == 1 else series.get("title", "?")))
    elif "movie" in p:
        movie = p.get("movie") or {}
        f = p.get("movieFile") or {}
        path = f.get("path") or (os.path.join(movie["folderPath"], f["relativePath"])
                                 if movie.get("folderPath") and f.get("relativePath") else None)
        year = movie.get("year")
        if path:
            out.append((path, (movie.get("title") or "?") + (f" ({year})" if year else "")))
    return out


def _queue(files, cfg):
    for arr_path, title in files:
        local = scanner.remap_path(arr_path, cfg)
        try:
            scanner.invalidate(local)          # a fresh import: never trust a cached status
            info = media.classify(local, cfg)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            log.warning("Webhook: could not inspect %s: %s", local, e)
            continue
        if not info.get("translatable"):
            log.info("Webhook: %s — nothing to do (%s)", title, info.get("status"))
            continue
        added, jid = db.add_job(local, title, source="webhook")
        log.info("Webhook: %s %s", title, f"queued (job {jid})" if added else "already queued")


def handle(payload, cfg):
    """Answer at once; inspecting the file (ffprobe) happens in the background so
    Sonarr/Radarr never time out waiting for us. Returns (http_status, message)."""
    event = payload.get("eventType") or "?"
    if event == "Test":
        log.info("Webhook: test event received from %s", payload.get("instanceName") or "Sonarr/Radarr")
        return 200, "ok"
    files = files_from_payload(payload)
    if not files:
        return 200, f"ignored ({event})"
    threading.Thread(target=_queue, args=(files, cfg), name="webhook", daemon=True).start()
    return 202, f"checking {len(files)} file(s)"
