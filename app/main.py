#!/usr/bin/env python3
"""translAItarr2 — web app entry point.

Boots Flask, gates first-run setup behind a wizard, and serves the three main
pages (Library / Queue / Settings). The *arr client, library scanner and the
translation engine are wired in as those modules land; this file owns app
bootstrapping, the setup wizard, and authentication.
"""
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
PUBLIC_ENDPOINTS = {"health", "static", "login", "setup", "setup_submit"}


@app.before_request
def gate():
    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_ENDPOINTS:
        return None

    cfg = cfgmod.load_config()
    if not cfg.get("onboarding_completed"):
        # First-run: the wizard's connectivity test is the only extra endpoint
        # reachable before a config (and thus auth) exists.
        if endpoint == "arr_test":
            return None
        return redirect(url_for("setup"))

    if cfg.get("auth", {}).get("enabled") and not session.get("authed"):
        # arr_test is fetched via JS — answer with JSON 401 instead of a redirect.
        if endpoint == "arr_test":
            return jsonify({"ok": False, "message": "Authentication required"}), 401
        return redirect(url_for("login"))
    return None


@app.context_processor
def inject_version():
    # Cache-only read; the background thread does the GitHub fetch.
    return {"version": version.info()}


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

    models = [m.strip() for m in f.get("gemini_models", "").splitlines() if m.strip()]
    if models:
        cfg["gemini"]["models"] = models

    sources = [s.strip() for s in f.get("source_priority", "").replace(",", " ").split() if s.strip()]
    if sources:
        cfg["languages"]["source_priority"] = sources
    cfg["languages"]["target"]["name"] = f.get("target_name", "Czech").strip() or "Czech"
    cfg["languages"]["target"]["code"] = f.get("target_code", "cs").strip() or "cs"

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
    return render_template("library.html", titles=rows, active="library")


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


_STATUS_CHIP = {"pending": "amber", "processing": "blue", "done": "green",
                "error": "red", "skipped": "gray"}


def _log_tail(n=80):
    try:
        with open(cfgmod.LOG_FILE, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]) or "No activity yet."
    except OSError:
        return "No activity yet."


@app.route("/queue")
def queue():
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
    }
    return render_template("queue.html", jobs=jobs, usage=usage, log=_log_tail(), active="queue")


@app.route("/translate", methods=["POST"])
def translate():
    path = request.form.get("path", "")
    title = request.form.get("title", "")
    if not path:
        flash("No file path provided.")
        return redirect(url_for("library"))
    added, info = db.add_job(path, title, source="manual")
    flash(f"Queued: {title or path}" if added else f"Already queued (job {info}).")
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


@app.route("/settings")
def settings():
    return render_template("settings.html", cfg=cfgmod.redact(cfgmod.load_config()), active="settings")


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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

    models = [m.strip() for m in f.get("gemini_models", "").splitlines() if m.strip()]
    if models:
        cfg["gemini"]["models"] = models
    sources = [s for s in f.get("source_priority", "").replace(",", " ").split() if s]
    if sources:
        cfg["languages"]["source_priority"] = sources
    cfg["languages"]["target"]["name"] = f.get("target_name", "").strip() or "Czech"
    cfg["languages"]["target"]["code"] = f.get("target_code", "").strip() or "cs"

    cfg["automation"]["enabled"] = f.get("automation_enabled") == "on"
    cfg["automation"]["scan_interval_minutes"] = _int(f.get("scan_interval"), cfg["automation"]["scan_interval_minutes"])

    for k in ("max_daily_per_model", "max_daily_total", "max_per_run"):
        cfg["limits"][k] = _int(f.get(k), cfg["limits"][k])

    for k in ("brackets", "parens", "music", "speaker", "uppercase"):
        cfg["sdh"][k] = f.get(f"sdh_{k}") == "on"

    cfg["translation"]["batch_size"] = _int(f.get("batch_size"), cfg["translation"]["batch_size"])
    cfg["translation"]["context_enabled"] = f.get("context_enabled") == "on"
    cfg["translation"]["context_before"] = _int(f.get("context_before"), cfg["translation"]["context_before"])
    cfg["translation"]["context_after"] = _int(f.get("context_after"), cfg["translation"]["context_after"])
    cfg["translation"]["add_translator_credit"] = f.get("add_translator_credit") == "on"

    cfgmod.save_config(cfg)
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
