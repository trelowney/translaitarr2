"""Background worker: pulls pending jobs and runs the translation engine.

A single daemon thread processes the queue serially (Gemini free-tier limits make
parallelism pointless). It respects the daily request ceiling and, when every
model is rate-limited, requeues the job and sleeps until the next quota reset.
"""
import logging
import os
import threading
import time

import config as cfgmod
import db
import media
import scanner
import translator

log = logging.getLogger("translaitarr2")

_started = False


def _verify_label(r):
    if r.get("ok"):
        return f"verify ✓ ({r.get('checked', 0)} cues)"
    if "bad" in r:
        return f"verify ⚠ {r.get('bad', 0)} issue(s)"
    return "verify: " + r.get("note", "failed")


def _verify_note(r):
    """Human-readable detail of the cues the verifier flagged, for the Queue UI."""
    flagged = r.get("flagged") or []
    if not flagged:
        return None
    return "\n".join(f"• {f['source']}  →  {f['translation']}" for f in flagged)


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

        job_id, path, title, force, action, provider = job
        cfg = cfgmod.load_config()

        # Per-job provider override: pin this job to one provider, no fallback.
        if provider and provider in translator.PROVIDERS:
            cfg["ai"] = {"primary": provider, "secondary": "none", "tertiary": "none"}

        chain = translator.provider_chain(cfg)
        if not chain or not any(translator.provider_configured(cfg, p) for p, *_ in chain):
            who = provider or (chain[0][0] if chain else "any")
            log.warning("No usable translation provider configured (%s) — worker idle", who)
            time.sleep(30)
            continue

        max_total = cfg["limits"].get("max_daily_total", 120)
        if db.today_total() >= max_total:
            secs = db.seconds_until_reset(cfg["automation"].get("rpd_reset_tz", "UTC"))
            log.warning("Daily limit reached (%s/%s) — sleeping %ss until reset",
                        db.today_total(), max_total, secs)
            time.sleep(secs)
            continue

        log.info("Job %s: starting [%s] — %s", job_id, action, title or path)
        db.set_status(job_id, "processing")
        try:
            usage = db.today_per_model()
            fails = {}
            if action == "verify":
                vres, model_calls = translator.verify_translation(path, cfg, usage=usage)
                db.record_calls(model_calls)
                db.set_status(job_id, "done", result=_verify_label(vres), verify_note=_verify_note(vres))
                log.info("Job %s: %s", job_id, _verify_label(vres))
            else:
                outcome, model_calls = translator.translate_file(path, cfg, force=force, usage=usage, fails=fails)
                if outcome == "translated":
                    extra, note = "", None
                    if cfg["translation"].get("verify"):
                        vres, vcalls = translator.verify_translation(path, cfg, usage=usage)
                        for m, n in vcalls.items():
                            model_calls[m] = model_calls.get(m, 0) + n
                        extra = " · " + _verify_label(vres)
                        note = _verify_note(vres)
                    total = db.record_calls(model_calls)
                    db.record_fails(fails)
                    db.set_status(job_id, "done", result="translated" + extra, verify_note=note)
                    # Remember the sidecar we wrote so superseded-cleanup can later
                    # remove it safely (and only ours) if the release gains the target language.
                    db.record_sidecar(media.target_sidecar_path(path, cfg["languages"]["target"]["code"]))
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

        db.prune_jobs(20)
        time.sleep(3)


def _cleanup_superseded(rows, cfg):
    """Remove a translated sidecar we wrote once a release upgrade already ships the
    target language itself (embedded audio or subtitle). Safety: only deletes files
    translAItarr2 created (tracked in db.our_sidecars) and only when the sidecar
    predates the current release — it never touches user-provided subtitles."""
    target = cfg["languages"]["target"]
    code = target["code"]
    name = target.get("name") or code.upper()
    removed = 0
    for r in rows:
        # reason == "embedded" means the current release natively carries the
        # target language; classify only reaches that verdict when any sidecar is stale.
        if r.get("reason") != "embedded":
            continue
        sidecar = media.target_sidecar_path(r["local_path"], code)
        if not db.is_our_sidecar(sidecar) or not os.path.exists(sidecar):
            continue
        try:
            if os.path.getmtime(sidecar) >= os.path.getmtime(r["local_path"]):
                continue  # not stale — current release, keep it
        except OSError:
            continue
        try:
            os.remove(sidecar)
            db.forget_sidecar(sidecar)
            scanner.invalidate(r["local_path"])
            removed += 1
            log.info("Cleanup: removed superseded %s — release now ships %s natively",
                     os.path.basename(sidecar), name)
        except OSError as e:  # noqa: BLE001
            log.warning("Cleanup: could not remove %s: %s", sidecar, e)
    if removed:
        log.info("Cleanup: removed %s superseded sidecar(s)", removed)


def _automation_loop():
    """Periodic library maintenance. When auto-translate is enabled, queue any title
    missing the target language (up to max_per_run per cycle; files that previously
    errored are left for a manual retry). Independently, when cleanup_superseded is
    on, remove translated sidecars that an upgraded release has made redundant."""
    while True:
        cfg = cfgmod.load_config()
        auto = cfg.get("automation", {})
        cleanup = cfg["translation"].get("cleanup_superseded", True)
        if not auto.get("enabled") and not cleanup:
            time.sleep(60)  # nothing to do — re-check the toggles each minute
            continue

        try:
            rows, _ = scanner.scan(cfg)
            if cleanup:
                _cleanup_superseded(rows, cfg)
            if auto.get("enabled"):
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

        # Sleep the configured interval, but wake every 30s so a toggle change
        # (auto-translate on/off) takes effect promptly.
        interval = max(1, int(auto.get("scan_interval_minutes", 30))) * 60
        slept = 0
        while slept < interval:
            time.sleep(30)
            slept += 30
            if cfgmod.load_config().get("automation", {}).get("enabled") != auto.get("enabled"):
                break
