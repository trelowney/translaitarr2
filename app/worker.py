"""Background worker: pulls pending jobs and runs the translation engine.

A single daemon thread processes the queue serially (Gemini free-tier limits make
parallelism pointless). It respects the daily request ceiling and, when every
model is rate-limited, requeues the job and sleeps until the next quota reset.
"""
import logging
import threading
import time

import config as cfgmod
import db
import scanner
import translator

log = logging.getLogger("translaitarr2")

_started = False


def start():
    """Start the worker + automation threads once (idempotent)."""
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="worker", daemon=True).start()
    threading.Thread(target=_automation_loop, name="automation", daemon=True).start()
    log.info("Worker + automation threads started")


def _loop():
    while True:
        job = db.get_next_pending()
        if not job:
            time.sleep(10)
            continue

        job_id, path, title, force = job
        cfg = cfgmod.load_config()

        if not cfg["gemini"].get("api_key"):
            log.warning("No Gemini API key configured — worker idle")
            time.sleep(30)
            continue

        max_total = cfg["limits"].get("max_daily_total", 120)
        if db.today_total() >= max_total:
            secs = db.seconds_until_reset(cfg["automation"].get("rpd_reset_tz", "UTC"))
            log.warning("Daily limit reached (%s/%s) — sleeping %ss until reset",
                        db.today_total(), max_total, secs)
            time.sleep(secs)
            continue

        log.info("Job %s: starting — %s", job_id, title or path)
        db.set_status(job_id, "processing")
        try:
            outcome, model_calls = translator.translate_file(path, cfg, force=force)
            if outcome == "translated":
                total = db.record_calls(model_calls)
                db.set_status(job_id, "done", result="translated")
                log.info("Job %s: done (today %s/%s)", job_id, total, max_total)
            else:
                db.set_status(job_id, "done", result=outcome)
                log.info("Job %s: %s", job_id, outcome)
            scanner.invalidate(path)
        except translator.AllModelsExhaustedError as e:
            log.warning("Job %s: %s — requeuing, sleeping until reset", job_id, e)
            db.set_status(job_id, "pending")
            time.sleep(db.seconds_until_reset(cfg["automation"].get("rpd_reset_tz", "UTC")))
            continue
        except Exception as e:  # noqa: BLE001 - any failure must not kill the worker
            log.error("Job %s FAILED: %s", job_id, e)
            db.set_status(job_id, "error", error=str(e))

        time.sleep(3)


def _automation_loop():
    """When enabled, periodically scan the library and queue any title missing the
    target language (up to max_per_run per cycle). Files that previously errored
    are left for a manual retry rather than re-queued every cycle."""
    while True:
        cfg = cfgmod.load_config()
        auto = cfg.get("automation", {})
        if not auto.get("enabled"):
            time.sleep(60)  # re-check the toggle each minute
            continue

        try:
            rows, _ = scanner.scan(cfg)
            cap = cfg["limits"].get("max_per_run", 10)
            queued = 0
            for r in rows:
                if queued >= cap:
                    break
                if (r["translatable"]
                        and not db.has_errored_job(r["local_path"])
                        and db.add_job(r["local_path"], r["title"], source="auto")[0]):
                    queued += 1
            if queued:
                log.info("Automation: queued %s title(s)", queued)
        except Exception as e:  # noqa: BLE001 - keep the automation thread alive
            log.error("Automation scan failed: %s", e)

        # Sleep the configured interval, but wake every 30s so toggling the
        # switch off (or changing the interval) takes effect promptly.
        interval = max(1, int(auto.get("scan_interval_minutes", 30))) * 60
        slept = 0
        while slept < interval:
            time.sleep(30)
            slept += 30
            if not cfgmod.load_config().get("automation", {}).get("enabled"):
                break
