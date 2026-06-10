#!/usr/bin/env python3
"""translAItarr2 — web app entry point.

Boots Flask, gates first-run setup behind a wizard, and serves the three main
pages (Library / Queue / Settings). The *arr client, library scanner and the
translation engine are wired in as those modules land; this file owns app
bootstrapping, the setup wizard, and authentication.
"""
import json
import logging
import os
import secrets
import sys

from flask import (
    Flask, redirect, render_template, request, session, url_for, jsonify, flash,
)

import arr
import config as cfgmod
import db
import scanner
import stats
import translator
import version
import worker

# ── Logging (stdout for `docker logs` + a file in the config volume) ──────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("translaitarr2")
try:
    os.makedirs(cfgmod.CONFIG_DIR, exist_ok=True)
    fh = logging.FileHandler(cfgmod.LOG_FILE)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
except OSError as e:  # config volume not writable yet — stdout still works
    log.warning("Could not open log file: %s", e)

app = Flask(__name__)

# Persist a session secret in the config volume so logins survive restarts.
_secret_path = os.path.join(cfgmod.CONFIG_DIR, ".secret_key")
try:
    if os.path.exists(_secret_path):
        with open(_secret_path) as f:
            app.secret_key = f.read().strip()
    else:
        app.secret_key = secrets.token_hex(32)
        with open(_secret_path, "w") as f:
            f.write(app.secret_key)
        os.chmod(_secret_path, 0o600)
except OSError:
    app.secret_key = secrets.token_hex(32)

# Endpoints reachable without an active config / login.
# Changes each process start, so a rebuilt image busts the browser's cache of
# /static assets (appended as ?v=…).
ASSET_VER = secrets.token_hex(4)

PUBLIC_ENDPOINTS = {"health", "static", "login", "setup", "setup_submit"}
# JS helper endpoints the setup wizard needs before a config/auth exists.
WIZARD_API = {"arr_test", "gemini_models"}


@app.before_request
def gate():
    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_ENDPOINTS:
        return None

    cfg = cfgmod.load_config()
    if not cfg.get("onboarding_completed"):
        # First-run: wizard helper endpoints are reachable; everything else
        # redirects into the wizard.
        if endpoint in WIZARD_API:
            return None
        return redirect(url_for("setup"))

    if cfg.get("auth", {}).get("enabled") and not session.get("authed"):
        # Wizard APIs are fetched via JS — answer JSON 401 instead of redirecting.
        if endpoint in WIZARD_API:
            return jsonify({"ok": False, "message": "Authentication required"}), 401
        return redirect(url_for("login"))
    return None


@app.context_processor
def inject_version():
    # Cache-only read; the background thread does the GitHub fetch.
    return {"version": version.info()}


@app.context_processor
def inject_langs():
    return {"source_languages": cfgmod.SOURCE_LANGUAGES,
            "target_languages": cfgmod.TARGET_LANGUAGES}


@app.context_processor
def inject_assets():
    return {"asset_ver": ASSET_VER}


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = cfgmod.load_config()
    if not cfg.get("auth", {}).get("enabled"):
        return redirect(url_for("library"))
    if request.method == "POST":
        if cfgmod.verify_password(cfg, request.form.get("password", "")):
            session["authed"] = True
            return redirect(url_for("library"))
        flash("Incorrect password.")
    return render_template("login.html", show_nav=False)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── First-run setup wizard ────────────────────────────────────────────────────
@app.route("/setup")
def setup():
    if cfgmod.load_config().get("onboarding_completed"):
        return redirect(url_for("library"))
    return render_template("wizard.html", cfg=cfgmod.load_config(), show_nav=False)


@app.route("/setup", methods=["POST"], endpoint="setup_submit")
def setup_submit():
    cfg = cfgmod.load_config()
    f = request.form

    cfg["arr"]["sonarr"]["url"] = f.get("sonarr_url", "").strip()
    cfg["arr"]["sonarr"]["api_key"] = f.get("sonarr_api_key", "").strip()
    cfg["arr"]["radarr"]["url"] = f.get("radarr_url", "").strip()
    cfg["arr"]["radarr"]["api_key"] = f.get("radarr_api_key", "").strip()
    cfg["gemini"]["api_key"] = f.get("gemini_api_key", "").strip()

    _apply_lang_model_fields(cfg, f)

    if f.get("auth_enabled") == "on" and f.get("password"):
        cfg["auth"]["enabled"] = True
        cfg["auth"]["password_hash"] = cfgmod.hash_password(f["password"])
        session["authed"] = True
    else:
        cfg["auth"]["enabled"] = False
        cfg["auth"]["password_hash"] = ""

    cfg["onboarding_completed"] = True
    cfgmod.save_config(cfg)
    log.info("Setup wizard completed; config written.")
    return redirect(url_for("library"))


# ── Pages (placeholders until the scanner/engine are wired in) ─────────────────
@app.route("/")
def library():
    rows, errors = scanner.scan(cfgmod.load_config())
    for e in errors:
        flash(e)
    movies = [r for r in rows if r["kind"] == "Movie"]
    episodes = [r for r in rows if r["kind"] == "Episode"]
    return render_template("library.html", movies=movies, episodes=episodes, active="library")


@app.route("/rescan", methods=["POST"])
def rescan():
    _, errors = scanner.scan(cfgmod.load_config(), force=True)
    flash("Rescan complete." if not errors else "Rescan finished with errors: " + "; ".join(errors))
    return redirect(url_for("library"))


@app.route("/api/arr/test", methods=["POST"], endpoint="arr_test")
def arr_test():
    data = request.get_json(silent=True) or request.form
    service = (data.get("service") or "").lower()
    client_cls = arr.RadarrClient if service == "radarr" else arr.SonarrClient
    ok, message = client_cls(data.get("url", ""), data.get("api_key", "")).test()
    return jsonify({"ok": ok, "message": message})


@app.route("/api/gemini/models", methods=["POST"], endpoint="gemini_models")
def gemini_models():
    data = request.get_json(silent=True) or request.form
    key = (data.get("api_key") or "").strip() or cfgmod.load_config()["gemini"].get("api_key", "")
    if not key:
        return jsonify({"ok": False, "error": "Enter a Gemini API key first."}), 200
    try:
        return jsonify({"ok": True, "models": translator.list_available_models(key)})
    except Exception as e:  # noqa: BLE001 - surface any API/network error to the UI
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/arr/rootfolders", methods=["POST"], endpoint="arr_rootfolders")
def arr_rootfolders():
    return jsonify({"folders": arr.all_root_folders(cfgmod.load_config())})


_STATUS_CHIP = {"pending": "amber", "processing": "blue", "done": "green",
                "error": "red", "skipped": "gray"}


def _log_tail(n=120):
    """Last n log lines, newest first."""
    try:
        with open(cfgmod.LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        return "".join(reversed(lines)) or "No activity yet."
    except OSError:
        return "No activity yet."


def _queue_data():
    cfg = cfgmod.load_config()
    jobs = []
    for j in db.list_jobs():
        result = j.get("result") or ""
        # A 'done' job that was skipped reads as e.g. "skipped:embedded".
        status = "skipped" if result.startswith("skipped") else j["status"]
        jobs.append({
            "id": j["id"],
            "title": j["title"] or j["file_path"],
            "status": status,
            "chip": _STATUS_CHIP.get(status, "gray"),
            "detail": j.get("error") or result,
        })
    usage = {
        "total": db.today_total(),
        "limit": cfg["limits"].get("max_daily_total", 120),
        "per_model": db.today_per_model(),
        "outcomes": db.outcome_counts(),
        "system": stats.container_stats(),
    }
    return jobs, usage, _log_tail()


@app.route("/queue")
def queue():
    jobs, usage, log = _queue_data()
    return render_template("queue.html", jobs=jobs, usage=usage, log=log, active="queue")


@app.route("/api/queue")
def api_queue():
    jobs, usage, log = _queue_data()
    return jsonify({"jobs": jobs, "usage": usage, "log": log})


@app.route("/translate", methods=["POST"])
def translate():
    path = request.form.get("path", "")
    title = request.form.get("title", "")
    if not path:
        flash("No file path provided.")
        return redirect(url_for("library"))
    force = request.form.get("force") == "1"
    added, info = db.add_job(path, title, source="manual", force=force)
    if added:
        flash(f"Queued for re-translation: {title or path}" if force else f"Queued: {title or path}")
    else:
        flash(f"Already queued (job {info}).")
    return redirect(url_for("queue"))


@app.route("/verify", methods=["POST"])
def verify():
    path = request.form.get("path", "")
    title = request.form.get("title", "")
    if not path:
        flash("No file path provided.")
        return redirect(url_for("library"))
    added, info = db.add_job(path, title, source="manual", action="verify")
    flash(f"Queued for verification: {title or path}" if added else f"Already queued (job {info}).")
    return redirect(url_for("queue"))


@app.route("/translate-all", methods=["POST"])
def translate_all():
    rows, _ = scanner.scan(cfgmod.load_config())
    n = 0
    for r in rows:
        if r["translatable"] and db.add_job(r["local_path"], r["title"], source="manual")[0]:
            n += 1
    flash(f"Queued {n} title(s) for translation.")
    return redirect(url_for("queue"))


@app.route("/retry/<int:job_id>", methods=["POST"])
def retry(job_id):
    db.retry(job_id)
    flash(f"Job {job_id} re-queued.")
    return redirect(url_for("queue"))


@app.route("/job/<int:job_id>/delete", methods=["POST"])
def delete_job(job_id):
    db.delete_job(job_id)
    return ("", 204) if request.headers.get("X-Requested-With") == "fetch" else redirect(url_for("queue"))


@app.route("/queue/clear", methods=["POST"])
def clear_finished():
    n = db.clear_finished()
    flash(f"Cleared {n} finished job(s).")
    return redirect(url_for("queue"))


@app.route("/settings")
def settings():
    return render_template("settings.html", cfg=cfgmod.redact(cfgmod.load_config()), active="settings")


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_models(raw):
    try:
        return [str(x).strip() for x in json.loads(raw) if str(x).strip()]
    except (ValueError, TypeError):
        return [m.strip() for m in (raw or "").replace(",", "\n").splitlines() if m.strip()]


def _apply_lang_model_fields(cfg, f):
    """Apply the clickable source langs (checkboxes), target lang (dropdown) and
    drag-ordered models (hidden JSON) shared by the wizard and Settings."""
    selected = set(f.getlist("source_priority"))
    ordered = [code for code, _ in cfgmod.SOURCE_LANGUAGES if code in selected]
    if ordered:
        cfg["languages"]["source_priority"] = ordered
    target_code = (f.get("target_code") or "").strip()
    if target_code:
        cfg["languages"]["target"]["code"] = target_code
        cfg["languages"]["target"]["name"] = cfgmod.target_name_for(target_code)
    models = _parse_models(f.get("models", ""))
    if models:
        cfg["gemini"]["models"] = models
        try:
            raw_batch = json.loads(f.get("model_batch", "{}"))
        except (ValueError, TypeError):
            raw_batch = {}
        cfg["gemini"]["model_batch"] = {
            m: _int(raw_batch[m], cfg["translation"]["batch_size"])
            for m in models if m in raw_batch
        }
        try:
            raw_limit = json.loads(f.get("model_daily_limit", "{}"))
        except (ValueError, TypeError):
            raw_limit = {}
        cfg["gemini"]["model_daily_limit"] = {
            m: _int(raw_limit[m], cfg["limits"]["max_daily_per_model"])
            for m in models if m in raw_limit
        }


@app.route("/settings", methods=["POST"], endpoint="settings_save")
def settings_save():
    cfg = cfgmod.load_config()
    f = request.form

    cfg["arr"]["sonarr"]["url"] = f.get("sonarr_url", cfg["arr"]["sonarr"]["url"]).strip()
    cfg["arr"]["radarr"]["url"] = f.get("radarr_url", cfg["arr"]["radarr"]["url"]).strip()
    # Blank secret field => keep the existing key.
    if f.get("sonarr_api_key", "").strip():
        cfg["arr"]["sonarr"]["api_key"] = f["sonarr_api_key"].strip()
    if f.get("radarr_api_key", "").strip():
        cfg["arr"]["radarr"]["api_key"] = f["radarr_api_key"].strip()
    if f.get("gemini_api_key", "").strip():
        cfg["gemini"]["api_key"] = f["gemini_api_key"].strip()

    _apply_lang_model_fields(cfg, f)

    cfg["automation"]["enabled"] = f.get("automation_enabled") == "on"
    cfg["automation"]["scan_interval_minutes"] = _int(f.get("scan_interval"), cfg["automation"]["scan_interval_minutes"])

    for k in ("max_daily_per_model", "max_daily_total", "max_per_run"):
        cfg["limits"][k] = _int(f.get(k), cfg["limits"][k])

    for k in ("brackets", "parens", "music", "speaker", "uppercase"):
        cfg["sdh"][k] = f.get(f"sdh_{k}") == "on"

    # Path remap rules — one "arr_path => local_path" per line.
    remap = []
    for line in f.get("path_remap", "").splitlines():
        if "=>" in line:
            a, b = line.split("=>", 1)
            if a.strip() and b.strip():
                remap.append({"from": a.strip(), "to": b.strip()})
    cfg["paths"]["remap"] = remap

    cfg["validation"]["enabled"] = f.get("validation_enabled") == "on"
    for k in ("min_chars", "max_chars", "min_duration_ms", "max_duration_s"):
        cfg["validation"][k] = _int(f.get(k), cfg["validation"][k])

    if f.get("source_preference") in ("video", "sidecar"):
        cfg["translation"]["source_preference"] = f["source_preference"]
    cfg["translation"]["verify"] = f.get("verify_enabled") == "on"
    cfg["translation"]["verify_samples"] = _int(f.get("verify_samples"), cfg["translation"]["verify_samples"])

    cfgmod.save_config(cfg)
    # Auto-save (fetch) requests get a quiet 204; full form posts redirect.
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    flash("Settings saved.")
    return redirect(url_for("settings"))


# ── Status (redacted — safe to expose) ────────────────────────────────────────
@app.route("/status")
def status():
    return jsonify({"config": cfgmod.redact(cfgmod.load_config())})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9878"))
    log.info("=" * 56)
    log.info("translAItarr2 starting on port %s", port)
    log.info("=" * 56)
    db.init_db()
    worker.start()
    version.start()
    try:
        from waitress import serve  # production WSGI server
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        log.warning("waitress not installed — falling back to Flask dev server")
        app.run(host="0.0.0.0", port=port, use_reloader=False)
