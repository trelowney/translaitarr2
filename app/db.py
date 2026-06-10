"""SQLite job queue + daily Gemini usage counters (stored in the config volume).

Kept deliberately small: a single jobs table plus two counter tables. Every
helper opens its own short-lived connection so the worker thread and the request
threads never share one (sqlite connections are not thread-safe).
"""
import logging
import sqlite3
from datetime import datetime, timedelta

import config as cfgmod

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger("translaitarr2")


def get_db():
    conn = sqlite3.connect(cfgmod.DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT NOT NULL,
            title       TEXT DEFAULT '',
            source      TEXT DEFAULT 'manual',
            status      TEXT DEFAULT 'pending',
            force       INTEGER DEFAULT 0,
            action      TEXT DEFAULT 'translate',
            added_at    TEXT DEFAULT (datetime('now')),
            started_at  TEXT,
            finished_at TEXT,
            result      TEXT,
            error       TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_count (
            day   TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS model_daily_calls (
            day   TEXT NOT NULL,
            model TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            fails INTEGER DEFAULT 0,
            PRIMARY KEY (day, model)
        );
        """
    )
    # Migrate DBs created before the 'force' column existed.
    for ddl in ("ALTER TABLE jobs ADD COLUMN force INTEGER DEFAULT 0",
                "ALTER TABLE jobs ADD COLUMN action TEXT DEFAULT 'translate'",
                "ALTER TABLE model_daily_calls ADD COLUMN fails INTEGER DEFAULT 0"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


# ── Jobs ──────────────────────────────────────────────────────────────────────

def add_job(file_path, title="", source="manual", force=False, action="translate"):
    """Queue a file. Returns (added, id). Skips if already pending/processing
    for the same action."""
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM jobs WHERE file_path=? AND action=? AND status IN ('pending','processing')",
        (file_path, action),
    ).fetchone()
    if existing:
        conn.close()
        return False, existing["id"]
    cur = conn.execute(
        "INSERT INTO jobs (file_path, title, source, force, action) VALUES (?, ?, ?, ?, ?)",
        (file_path, title, source, 1 if force else 0, action),
    )
    conn.commit()
    jid = cur.lastrowid
    conn.close()
    return True, jid


def reset_stuck_jobs():
    """On startup, return jobs left 'processing' (e.g. killed mid-run) to 'pending'."""
    conn = get_db()
    conn.execute("UPDATE jobs SET status='pending', started_at=NULL WHERE status='processing'")
    n = conn.total_changes
    conn.commit()
    conn.close()
    if n:
        log.info("Reset %s stuck job(s) to pending", n)
    return n


def prune_jobs(keep=20):
    """Keep only the newest `keep` finished (done/error) jobs; drop older ones."""
    conn = get_db()
    conn.execute(
        """DELETE FROM jobs WHERE status IN ('done','error') AND id NOT IN
           (SELECT id FROM jobs WHERE status IN ('done','error') ORDER BY id DESC LIMIT ?)""",
        (keep,),
    )
    conn.commit()
    conn.close()


def get_next_pending():
    conn = get_db()
    row = conn.execute(
        "SELECT id, file_path, title, force, action FROM jobs WHERE status='pending' ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    return (row["id"], row["file_path"], row["title"], bool(row["force"]), row["action"]) if row else None


def set_status(job_id, status, result=None, error=None):
    conn = get_db()
    if status == "processing":
        conn.execute("UPDATE jobs SET status=?, started_at=datetime('now') WHERE id=?", (status, job_id))
    else:
        conn.execute(
            "UPDATE jobs SET status=?, finished_at=datetime('now'), result=?, error=? WHERE id=?",
            (status, result, error, job_id),
        )
    conn.commit()
    conn.close()


def list_jobs(limit=50):
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()]
    conn.close()
    return rows


def delete_job(job_id):
    conn = get_db()
    conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


def clear_finished():
    """Remove all done/error/skipped jobs. Returns how many were removed."""
    conn = get_db()
    conn.execute("DELETE FROM jobs WHERE status IN ('done', 'error')")
    n = conn.total_changes
    conn.commit()
    conn.close()
    return n


def has_errored_job(path):
    """True if this file has a job in the 'error' state (so automation skips it
    and leaves it for a manual retry instead of looping)."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE file_path=? AND status='error' LIMIT 1", (path,)
    ).fetchone()
    conn.close()
    return row is not None


def retry(job_id):
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET status='pending', error=NULL, started_at=NULL, finished_at=NULL WHERE id=?",
        (job_id,),
    )
    conn.commit()
    conn.close()


# ── Daily usage counters ──────────────────────────────────────────────────────

def _today():
    return datetime.now().date().isoformat()


def record_calls(model_calls):
    conn = get_db()
    total = 0
    for model, n in model_calls.items():
        conn.execute(
            """INSERT INTO model_daily_calls (day, model, count) VALUES (?, ?, ?)
               ON CONFLICT(day, model) DO UPDATE SET count = count + excluded.count""",
            (_today(), model, n),
        )
        total += n
    conn.execute(
        """INSERT INTO daily_count (day, count) VALUES (?, ?)
           ON CONFLICT(day) DO UPDATE SET count = count + excluded.count""",
        (_today(), total),
    )
    conn.commit()
    conn.close()
    return total


def today_total():
    conn = get_db()
    row = conn.execute("SELECT count FROM daily_count WHERE day=?", (_today(),)).fetchone()
    conn.close()
    return row["count"] if row else 0


def outcome_counts():
    """Lifetime job outcome tally: translated / skipped / failed."""
    conn = get_db()
    rows = conn.execute("SELECT status, result FROM jobs").fetchall()
    conn.close()
    out = {"translated": 0, "skipped": 0, "errors": 0}
    for r in rows:
        if r["status"] == "error":
            out["errors"] += 1
        elif (r["result"] or "").startswith("skipped"):
            out["skipped"] += 1
        elif r["result"] == "translated":
            out["translated"] += 1
    return out


def record_fails(model_fails):
    """Per-model count of failed batch attempts (429/5xx/network) for today."""
    conn = get_db()
    for model, n in model_fails.items():
        if not n:
            continue
        conn.execute(
            """INSERT INTO model_daily_calls (day, model, count, fails) VALUES (?, ?, 0, ?)
               ON CONFLICT(day, model) DO UPDATE SET fails = fails + excluded.fails""",
            (_today(), model, n),
        )
    conn.commit()
    conn.close()


def today_per_model():
    """Successful call count per model today (used for the per-model daily limit)."""
    conn = get_db()
    rows = conn.execute("SELECT model, count FROM model_daily_calls WHERE day=?", (_today(),)).fetchall()
    conn.close()
    return {r["model"]: r["count"] for r in rows}


def today_model_stats():
    """Per-model {ok, fail} batch counts today, for display."""
    conn = get_db()
    rows = conn.execute("SELECT model, count, fails FROM model_daily_calls WHERE day=?", (_today(),)).fetchall()
    conn.close()
    return {r["model"]: {"ok": r["count"], "fail": r["fails"]} for r in rows}


def seconds_until_reset(tz_name="UTC"):
    """Seconds until the next local midnight in tz_name (Gemini RPD reset)."""
    tz = ZoneInfo(tz_name) if ZoneInfo else None
    try:
        now = datetime.now(tz)
    except Exception:
        now = datetime.now()
    nxt = now.replace(hour=0, minute=0, second=10, microsecond=0)
    if now >= nxt:
        nxt += timedelta(days=1)
    return max(60, int((nxt - now).total_seconds()))
