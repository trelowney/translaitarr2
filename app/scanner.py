"""Library scanner: map *arr titles to local files and compute subtitle status.

Results are cached by (path, mtime) in the queue DB, so a repeat scan only pays
for an os.stat() per file — full ffprobe runs only when a file is new or changed.
This keeps the Library page snappy and lets automation cheaply find work.
"""
import logging
import os
import sqlite3
import time

import arr
import config as cfgmod
import media

log = logging.getLogger("translaitarr2")


def _db():
    conn = sqlite3.connect(cfgmod.DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scan_cache (
            path         TEXT PRIMARY KEY,
            mtime        REAL,
            status       TEXT,
            chip         TEXT,
            translatable INTEGER,
            reason       TEXT,
            checked_at   REAL
        )"""
    )
    return conn


def remap_path(path, cfg):
    """Apply path-remap rules (arr path -> local mount). Identity by default."""
    for rule in cfg.get("paths", {}).get("remap", []):
        frm, to = rule.get("from"), rule.get("to")
        if frm and path.startswith(frm):
            return to + path[len(frm):]
    return path


def _status_for(path, cfg, conn, force=False):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = -1.0
    if not force:
        row = conn.execute(
            "SELECT * FROM scan_cache WHERE path=? AND mtime=?", (path, mtime)
        ).fetchone()
        if row:
            return dict(row)

    info = media.classify(path, cfg)
    conn.execute(
        """INSERT INTO scan_cache (path, mtime, status, chip, translatable, reason, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             mtime=excluded.mtime, status=excluded.status, chip=excluded.chip,
             translatable=excluded.translatable, reason=excluded.reason,
             checked_at=excluded.checked_at""",
        (path, mtime, info["status"], info["chip"], int(info["translatable"]),
         info["reason"], time.time()),
    )
    conn.commit()
    return {"path": path, "mtime": mtime, **info}


def scan(cfg=None, force=False):
    """Return (rows, errors). Each row = an *arr title plus its local path and
    subtitle status. Uses cached status unless ``force`` is set.
    """
    cfg = cfg or cfgmod.load_config()
    titles, errors = arr.list_all_titles(cfg)
    conn = _db()
    rows = []
    for t in titles:
        local = remap_path(t["path"], cfg)
        st = _status_for(local, cfg, conn, force=force)
        rows.append({
            **t,
            "local_path": local,
            "status": st["status"],
            "chip": st["chip"],
            "translatable": bool(st.get("translatable")),
        })
    conn.close()
    return rows, errors
