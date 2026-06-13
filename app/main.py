#!/usr/bin/env python3
"""translAItarr2 — web app entry point.

Boots Flask, gates first-run setup behind a wizard, and serves the three main
pages (Library / Queue / Settings). The *arr client, library scanner and the
translation engine are wired in as those modules land; this file owns app
bootstrapping, the setup wizard, and authentication.
"""
import html
import json
import logging
import os
import re
import secrets
import sys
import time
from logging.handlers import RotatingFileHandler

from datetime import datetime, timedelta, timezone

import requests

from markupsafe import Markup

from flask import (
    Flask, redirect, render_template, request, session, url_for, jsonify, flash,
)

import arr
import config as cfgmod
import db
import scanner
import stats
import telemetry
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
    fh = RotatingFileHandler(cfgmod.LOG_FILE, maxBytes=2_000_000, backupCount=3)
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


def _apply_session_lifetime(cfg):
    """Make the login cookie a persistent (not browser-session) cookie so it
    survives the browser being closed / mobile tabs being evicted. Read from
    config each login so a Settings change takes effect without a restart."""
    days = cfg.get("auth", {}).get("session_days", 30)
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = 30
    app.permanent_session_lifetime = timedelta(days=days)

# Endpoints reachable without an active config / login.
# Changes each process start, so a rebuilt image busts the browser's cache of
# /static assets (appended as ?v=…).
ASSET_VER = secrets.token_hex(4)

# Display names for the per-job provider picker (match the Settings slot labels).
PROVIDER_LABELS = {
    "gemini": "Gemini", "openrouter": "OpenRouter", "openai_compat": "OpenAI-compatible",
    "anthropic": "Anthropic (Claude)", "cloudflare": "Cloudflare Workers AI",
    "deepl": "DeepL", "libretranslate": "LibreTranslate", "google": "Google Translate",
    "azure": "Microsoft / Azure", "yandex": "Yandex", "cf_m2m100": "Cloudflare m2m100",
    "gtranslate_free": "Google Translate (free)",
}

PUBLIC_ENDPOINTS = {"health", "static", "login", "setup", "setup_submit", "favicon"}
# JS helper endpoints the setup wizard needs before a config/auth exists.
WIZARD_API = {"arr_test", "gemini_models", "gemini_test",
              "openrouter_models", "openrouter_test",
              "openai_models", "openai_test",
              "anthropic_models", "anthropic_test",
              "cloudflare_models", "cloudflare_test",
              "deepl_test", "libretranslate_test",
              "google_test", "azure_test", "yandex_test", "cfm2m_test", "gtfree_test"}


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
            _apply_session_lifetime(cfg)
            session.permanent = True
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

    # Translation engine (step 2): the chosen primary provider + its credentials.
    primary = (f.get("primary_provider") or "").strip()
    if primary in translator.PROVIDERS:
        cfg["ai"]["primary"] = primary
    _apply_provider_creds(cfg, f)

    _apply_lang_model_fields(cfg, f)

    if f.get("auth_enabled") == "on" and f.get("password"):
        cfg["auth"]["enabled"] = True
        cfg["auth"]["password_hash"] = cfgmod.hash_password(f["password"])
        _apply_session_lifetime(cfg)
        session.permanent = True
        session["authed"] = True
    else:
        cfg["auth"]["enabled"] = False
        cfg["auth"]["password_hash"] = ""

    cfg["automation"]["enabled"] = f.get("automation_enabled") == "on"
    cfg["onboarding_completed"] = True
    cfgmod.save_config(cfg)
    log.info("Setup wizard completed; config written.")
    return redirect(url_for("library"))


# ── Pages (placeholders until the scanner/engine are wired in) ─────────────────
@app.route("/favicon.ico")
def favicon():
    # Browsers/crawlers request this at the site root regardless of the <link> tags.
    return app.send_static_file("favicon.ico")


@app.route("/")
def library():
    cfg = cfgmod.load_config()
    rows, errors = scanner.scan(cfg)
    for e in errors:
        flash(e)
    movies = [r for r in rows if r["kind"] == "Movie"]
    episodes = [r for r in rows if r["kind"] == "Episode"]
    providers = [(p, PROVIDER_LABELS.get(p, p)) for p in translator.configured_providers(cfg)]
    return render_template("library.html", movies=movies, episodes=episodes,
                           providers=providers, active="library")


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


@app.route("/api/openrouter/models", methods=["POST"], endpoint="openrouter_models")
def openrouter_models():
    # Catalogue is a free public endpoint — a key is optional (and never spent here).
    data = request.get_json(silent=True) or request.form
    key = (data.get("api_key") or "").strip() or cfgmod.load_config()["openrouter"].get("api_key", "")
    try:
        models = translator.list_openrouter_models(key)
    except Exception as e:  # noqa: BLE001 - surface any API/network error to the UI
        return jsonify({"ok": False, "error": str(e)}), 200
    free = [m for m in models if m["free"]]
    paid = [m for m in models if not m["free"]]
    return jsonify({"ok": True, "free": free, "paid": paid, "count": len(models)})


@app.route("/api/openrouter/test", methods=["POST"], endpoint="openrouter_test")
def openrouter_test():
    data = request.get_json(silent=True) or request.form
    key = (data.get("api_key") or "").strip() or cfgmod.load_config()["openrouter"].get("api_key", "")
    if not key:
        return jsonify({"ok": False, "message": "Enter an OpenRouter API key first."})
    try:
        info = translator.openrouter_key_info(key)  # validates the key
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    # The account balance is more useful than the per-key limit (usually unset).
    try:
        cr = translator.openrouter_credits(key)
        msg = f"Connected — ${cr['total'] - cr['usage']:.2f} of ${cr['total']:.2f} credit left"
    except Exception:  # noqa: BLE001 - fall back to the key's own limit/usage
        limit, used = info.get("limit"), info.get("usage", 0) or 0
        if limit is None:
            msg = f"Connected — no spend limit on this key (used ${used:g})"
        else:
            msg = f"Connected — ${limit - used:g} of ${limit:g} key credit left"
    return jsonify({"ok": True, "message": msg})


@app.route("/api/openai/models", methods=["POST"], endpoint="openai_models")
def openai_models():
    data = request.get_json(silent=True) or request.form
    saved = cfgmod.load_config()["openai_compat"]
    base = (data.get("base_url") or "").strip() or saved.get("base_url", "")
    key = (data.get("api_key") or "").strip() or saved.get("api_key", "")
    if not base:
        return jsonify({"ok": False, "error": "Enter the server base URL first (e.g. http://host:11434/v1)."}), 200
    try:
        return jsonify({"ok": True, "models": translator.list_openai_models(base, key)})
    except requests.HTTPError as e:  # servers that expose /models still work as before
        code = getattr(e.response, "status_code", None)
        if code in (404, 405):  # e.g. Cloudflare Workers AI has no /models endpoint
            return jsonify({"ok": False, "error": "This server has no model-list endpoint — "
                            "type the model id(s) in manually. Translation still works."}), 200
        return jsonify({"ok": False, "error": str(e)}), 200
    except Exception as e:  # noqa: BLE001 - surface any other API/network error to the UI
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/openai/test", methods=["POST"], endpoint="openai_test")
def openai_test():
    data = request.get_json(silent=True) or request.form
    saved = cfgmod.load_config()["openai_compat"]
    base = (data.get("base_url") or "").strip() or saved.get("base_url", "")
    key = (data.get("api_key") or "").strip() or saved.get("api_key", "")
    if not base:
        return jsonify({"ok": False, "message": "Enter the server base URL first."})
    # First try the model-list endpoint…
    try:
        models = translator.list_openai_models(base, key)
        return jsonify({"ok": True, "message": f"Connected — {len(models)} model(s) available"})
    except Exception as models_err:  # noqa: BLE001
        pass
    # …some servers (e.g. Cloudflare Workers AI) don't expose /models. Fall back to a
    # tiny chat/completions probe with a configured model to confirm the real path works.
    probe = (saved.get("models") or [None])[0]
    if probe:
        cfg = cfgmod.load_config()
        cfg["openai_compat"]["base_url"] = base
        cfg["openai_compat"]["api_key"] = key
        try:
            translator._complete("openai_compat", probe, "Reply with just: OK", cfg, max_tokens=5)
            return jsonify({"ok": True, "message": f"Connected — no model list, but '{probe}' responds ✓"})
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "message": f"Reachable, but '{probe}' failed: {e}"})
    return jsonify({"ok": False, "message": f"Reachable, but no model-list endpoint — "
                    f"add a model id manually (translation still works). [{models_err}]"})


@app.route("/api/anthropic/models", methods=["POST"], endpoint="anthropic_models")
def anthropic_models():
    data = request.get_json(silent=True) or request.form
    saved = cfgmod.load_config()["anthropic"]
    key = (data.get("api_key") or "").strip() or saved.get("api_key", "")
    if not key:
        return jsonify({"ok": False, "error": "Enter your Anthropic API key first."}), 200
    try:
        models = translator.list_anthropic_models(key, saved.get("base_url", ""))
        return jsonify({"ok": True, "models": models})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/anthropic/test", methods=["POST"], endpoint="anthropic_test")
def anthropic_test():
    data = request.get_json(silent=True) or request.form
    saved = cfgmod.load_config()["anthropic"]
    key = (data.get("api_key") or "").strip() or saved.get("api_key", "")
    if not key:
        return jsonify({"ok": False, "message": "Enter your Anthropic API key first."})
    try:
        models = translator.list_anthropic_models(key, saved.get("base_url", ""))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    return jsonify({"ok": True, "message": f"Connected — {len(models)} model(s) available"})


@app.route("/api/cloudflare/models", methods=["POST"], endpoint="cloudflare_models")
def cloudflare_models():
    data = request.get_json(silent=True) or request.form
    saved = cfgmod.load_config()["cloudflare"]
    # the widget posts the account id in the url field, so accept either key
    acct = (data.get("account_id") or data.get("base_url") or "").strip() or saved.get("account_id", "")
    key = (data.get("api_key") or "").strip() or saved.get("api_key", "")
    if not acct or not key:
        return jsonify({"ok": False, "error": "Enter your Cloudflare account ID and API token first."}), 200
    try:
        return jsonify({"ok": True, "models": translator.list_cloudflare_models(acct, key)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/cloudflare/test", methods=["POST"], endpoint="cloudflare_test")
def cloudflare_test():
    data = request.get_json(silent=True) or request.form
    saved = cfgmod.load_config()["cloudflare"]
    acct = (data.get("account_id") or data.get("base_url") or "").strip() or saved.get("account_id", "")
    key = (data.get("api_key") or "").strip() or saved.get("api_key", "")
    if not acct or not key:
        return jsonify({"ok": False, "message": "Enter your Cloudflare account ID and API token first."})
    try:
        models = translator.list_cloudflare_models(acct, key)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    return jsonify({"ok": True, "message": f"Connected — {len(models)} text-generation model(s) available"})


@app.route("/api/deepl/test", methods=["POST"], endpoint="deepl_test")
def deepl_test():
    data = request.get_json(silent=True) or request.form
    key = (data.get("api_key") or "").strip() or cfgmod.load_config()["deepl"].get("api_key", "")
    if not key:
        return jsonify({"ok": False, "message": "Enter your DeepL API key first."})
    try:
        u = translator.deepl_usage(key)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    tier = "Free" if key.strip().endswith(":fx") else "Pro"
    if u["limit"]:
        left = u["limit"] - u["count"]
        msg = f"Connected ({tier}) — {left:,} of {u['limit']:,} characters left this period"
    else:
        msg = f"Connected ({tier}) — {u['count']:,} characters used"
    return jsonify({"ok": True, "message": msg})


@app.route("/api/libretranslate/test", methods=["POST"], endpoint="libretranslate_test")
def libretranslate_test():
    data = request.get_json(silent=True) or request.form
    saved = cfgmod.load_config()["libretranslate"]
    # the widget/test button posts the server URL in the url field
    base = (data.get("base_url") or "").strip() or saved.get("base_url", "")
    key = (data.get("api_key") or "").strip() or saved.get("api_key", "")
    if not base:
        return jsonify({"ok": False, "message": "Enter the LibreTranslate server URL first."})
    try:
        langs = translator.libretranslate_languages(base, key)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    target = cfgmod.load_config()["languages"]["target"]["code"]
    warn = "" if target in langs else f" (warning: target '{target}' not offered by this server)"
    return jsonify({"ok": True, "message": f"Connected — {len(langs)} languages available" + warn})


def _mt_key_test(provider, key_block, cfg_key_field="api_key"):
    """Shared MT 'Test' — translate a one-word probe to validate credentials."""
    data = request.get_json(silent=True) or request.form
    cfg = cfgmod.load_config()
    k = (data.get("api_key") or "").strip()
    if k:
        cfg[key_block][cfg_key_field] = k
    if not cfg[key_block].get(cfg_key_field):
        return jsonify({"ok": False, "message": f"Enter your {key_block} API key first."})
    try:
        out = translator.mt_probe(provider, cfg)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    return jsonify({"ok": True, "message": f"Connected — 'OK' → '{out}'"})


@app.route("/api/google/test", methods=["POST"], endpoint="google_test")
def google_test():
    return _mt_key_test("google", "google")


@app.route("/api/azure/test", methods=["POST"], endpoint="azure_test")
def azure_test():
    return _mt_key_test("azure", "azure")


@app.route("/api/yandex/test", methods=["POST"], endpoint="yandex_test")
def yandex_test():
    return _mt_key_test("yandex", "yandex")


@app.route("/api/cfm2m/test", methods=["POST"], endpoint="cfm2m_test")
def cfm2m_test():
    cfg = cfgmod.load_config()
    if not (cfg["cloudflare"].get("account_id") and cfg["cloudflare"].get("api_key")):
        return jsonify({"ok": False, "message": "Configure the Cloudflare tab (account ID + token) first."})
    try:
        out = translator.mt_probe("cf_m2m100", cfg)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    return jsonify({"ok": True, "message": f"Connected — 'OK' → '{out}'"})


@app.route("/api/gtfree/test", methods=["POST"], endpoint="gtfree_test")
def gtfree_test():
    try:
        out = translator.mt_probe("gtranslate_free", cfgmod.load_config())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})
    return jsonify({"ok": True, "message": f"Reachable — 'OK' → '{out}' (no key needed)"})


@app.route("/api/arr/rootfolders", methods=["POST"], endpoint="arr_rootfolders")
def arr_rootfolders():
    return jsonify({"folders": arr.all_root_folders(cfgmod.load_config())})


@app.route("/api/gemini/test", methods=["POST"], endpoint="gemini_test")
def gemini_test():
    data = request.get_json(silent=True) or request.form
    key = (data.get("api_key") or "").strip() or cfgmod.load_config()["gemini"].get("api_key", "")
    if not key:
        return jsonify({"ok": False, "message": "Enter a Gemini API key first."})
    try:
        models = translator.list_available_models(key)
        return jsonify({"ok": True, "message": f"Connected — {len(models)} models available"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(e)})


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


def _fmt_ts(ts):
    """SQLite UTC timestamp -> local 'MM-DD HH:MM'."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
        return dt.strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts


_PUSAGE = {"at": 0.0, "data": []}


def _provider_usage(cfg, ttl=600):
    """Live usage for providers that expose it (DeepL chars, OpenRouter credit).
    Cached — the Queue page polls every few seconds and must not hammer the APIs."""
    now = time.time()
    if now - _PUSAGE["at"] < ttl:
        return _PUSAGE["data"]
    out = []
    if cfg["deepl"].get("api_key"):
        try:
            u = translator.deepl_usage(cfg["deepl"]["api_key"])
            pct = f" ({u['count'] * 100 // u['limit']}%)" if u["limit"] else ""
            out.append({"name": "DeepL", "detail": f"{u['count']:,} / {u['limit']:,} chars{pct}"})
        except Exception:  # noqa: BLE001 - usage is best-effort, never break the queue
            pass
    if cfg["openrouter"].get("api_key"):
        try:
            c = translator.openrouter_credits(cfg["openrouter"]["api_key"])
            out.append({"name": "OpenRouter", "detail": f"${c['total'] - c['usage']:.2f} of ${c['total']:.2f} left"})
        except Exception:  # noqa: BLE001
            pass
    _PUSAGE["at"] = now
    _PUSAGE["data"] = out
    return out


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
            "added": _fmt_ts(j.get("added_at")),
            "finished": _fmt_ts(j.get("finished_at")),
            "detail": j.get("error") or result,
            "note": j.get("verify_note") or "",
        })
    usage = {
        "total": db.today_total(),
        "limit": cfg["limits"].get("max_daily_total", 120),
        "per_model": db.today_model_stats(),
        "outcomes": db.outcome_counts(),
        "system": stats.container_stats(),
        "providers": _provider_usage(cfg),
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
    provider = request.form.get("provider", "").strip()
    if provider not in translator.PROVIDERS:
        provider = ""
    added, info = db.add_job(path, title, source="manual", force=force, provider=provider)
    via = f" via {PROVIDER_LABELS.get(provider, provider)}" if provider else ""
    if added:
        flash((f"Queued for re-translation: {title or path}" if force else f"Queued: {title or path}") + via)
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
    provider = request.form.get("provider", "").strip()
    if provider not in translator.PROVIDERS:
        provider = ""
    n = 0
    for r in rows:
        if r["translatable"] and db.add_job(r["local_path"], r["title"], source="manual", provider=provider)[0]:
            n += 1
    via = f" via {PROVIDER_LABELS.get(provider, provider)}" if provider else ""
    flash(f"Queued {n} title(s) for translation{via}.")
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


def _apply_provider_models(cfg, f, provider, name_models, name_batch, name_limit,
                           require_present=False):
    """Apply a provider's drag-ordered model widget (models + per-model batch and
    daily-limit, all hidden JSON). When ``require_present`` is set an empty/missing
    list is ignored (keeps Gemini's defaults if the widget didn't post)."""
    if require_present and name_models not in f:
        return
    models = _parse_models(f.get(name_models, ""))
    if require_present and not models:
        return
    cfg[provider]["models"] = models
    try:
        raw_batch = json.loads(f.get(name_batch, "{}"))
    except (ValueError, TypeError):
        raw_batch = {}
    cfg[provider]["model_batch"] = {
        m: _int(raw_batch[m], cfg["translation"]["batch_size"])
        for m in models if m in raw_batch
    }
    try:
        raw_limit = json.loads(f.get(name_limit, "{}"))
    except (ValueError, TypeError):
        raw_limit = {}
    cfg[provider]["model_daily_limit"] = {
        m: _int(raw_limit[m], cfg["limits"]["max_daily_per_model"])
        for m in models if m in raw_limit
    }


# Non-secret companion fields for providers that need more than an API key.
_PROVIDER_EXTRA_FIELDS = {
    "openai_base_url": ("openai_compat", "base_url"),
    "cloudflare_account_id": ("cloudflare", "account_id"),
    "libretranslate_base_url": ("libretranslate", "base_url"),
    "azure_region": ("azure", "region"),
    "yandex_folder_id": ("yandex", "folder_id"),
}


def _set_nested(cfg, path, value):
    node = cfg
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value


def _apply_provider_creds(cfg, f):
    """Save any provider credentials present in the form, blank-guarded so an empty
    (hidden) wizard panel never wipes a value. Covers every provider key plus the
    non-secret companion fields (base URLs, account id, region, folder id)."""
    for field, path in SECRET_FIELDS.items():
        v = (f.get(field) or "").strip()
        if v:
            _set_nested(cfg, path, v)
    for field, path in _PROVIDER_EXTRA_FIELDS.items():
        v = (f.get(field) or "").strip()
        if v:
            _set_nested(cfg, path, v)


def _apply_lang_model_fields(cfg, f):
    """Apply the clickable source langs (checkboxes), target lang (dropdown) and
    drag-ordered Gemini models (hidden JSON) shared by the wizard and Settings."""
    selected = set(f.getlist("source_priority"))
    ordered = [code for code, _ in cfgmod.SOURCE_LANGUAGES if code in selected]
    if ordered:
        cfg["languages"]["source_priority"] = ordered
    target_code = (f.get("target_code") or "").strip()
    if target_code:
        cfg["languages"]["target"]["code"] = target_code
        cfg["languages"]["target"]["name"] = cfgmod.target_name_for(target_code)
    _apply_provider_models(cfg, f, "gemini", "models", "model_batch",
                           "model_daily_limit", require_present=True)


@app.route("/settings", methods=["POST"], endpoint="settings_save")
def settings_save():
    cfg = cfgmod.load_config()
    f = request.form

    cfg["arr"]["sonarr"]["url"] = f.get("sonarr_url", cfg["arr"]["sonarr"]["url"]).strip()
    cfg["arr"]["radarr"]["url"] = f.get("radarr_url", cfg["arr"]["radarr"]["url"]).strip()
    # API keys are NOT saved here — each has its own Save button (-> /api/secret),
    # so autosave never persists half-typed keys. Only the non-secret companion
    # fields (base URLs / region / ids) ride along with the autosave.
    if "openai_base_url" in f:
        cfg["openai_compat"]["base_url"] = f.get("openai_base_url", "").strip()
    if "cloudflare_account_id" in f:
        cfg["cloudflare"]["account_id"] = f.get("cloudflare_account_id", "").strip()
    if "libretranslate_base_url" in f:
        cfg["libretranslate"]["base_url"] = f.get("libretranslate_base_url", "").strip()
    if "azure_region" in f:
        cfg["azure"]["region"] = f.get("azure_region", "").strip()
    if "yandex_folder_id" in f:
        cfg["yandex"]["folder_id"] = f.get("yandex_folder_id", "").strip()

    _apply_lang_model_fields(cfg, f)
    _apply_provider_models(cfg, f, "openrouter", "or_models", "or_model_batch",
                           "or_model_daily_limit")
    _apply_provider_models(cfg, f, "openai_compat", "oc_models", "oc_model_batch",
                           "oc_model_daily_limit")
    _apply_provider_models(cfg, f, "anthropic", "an_models", "an_model_batch",
                           "an_model_daily_limit")
    _apply_provider_models(cfg, f, "cloudflare", "cf_models", "cf_model_batch",
                           "cf_model_daily_limit")

    # AI provider priority (primary first, then secondary, then tertiary).
    for slot in ("primary", "secondary", "tertiary"):
        v = (f.get(f"ai_{slot}") or "").strip()
        if v in translator.PROVIDERS or (slot != "primary" and v == "none"):
            cfg["ai"][slot] = v

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
    cfg["translation"]["cleanup_superseded"] = f.get("cleanup_superseded") == "on"

    cfg["telemetry"]["enabled"] = f.get("telemetry_enabled") == "on"

    cfgmod.save_config(cfg)
    # Auto-save (fetch) requests get a quiet 204; full form posts redirect.
    if request.headers.get("X-Requested-With") == "fetch":
        return ("", 204)
    flash("Settings saved.")
    return redirect(url_for("settings"))


@app.route("/settings/auth", methods=["POST"], endpoint="settings_auth")
def settings_auth():
    """Explicit (non-autosave) save for the Security card: enable/disable the
    password gate, set/change the password, and the session lifetime."""
    cfg = cfgmod.load_config()
    f = request.form
    new_pw = f.get("auth_password", "")
    if new_pw:
        if new_pw != f.get("auth_password_confirm", ""):
            flash("Passwords don't match — nothing changed.", "auth")
            return redirect(url_for("settings"))
        cfg["auth"]["password_hash"] = cfgmod.hash_password(new_pw)
    # Only enforce auth if a password actually exists, so ticking the box without
    # ever setting one can't lock everyone out.
    want = f.get("auth_enabled") == "on"
    if want and not cfg["auth"]["password_hash"]:
        flash("Set a password first to require sign-in.", "auth")
        return redirect(url_for("settings"))
    cfg["auth"]["enabled"] = bool(want and cfg["auth"]["password_hash"])
    cfg["auth"]["session_days"] = max(1, _int(f.get("session_days"), cfg["auth"].get("session_days", 30)))
    cfgmod.save_config(cfg)
    flash("Security settings saved.")
    return redirect(url_for("settings"))


# Form field name -> config location for the per-field API-key Save buttons.
SECRET_FIELDS = {
    "sonarr_api_key": ("arr", "sonarr", "api_key"),
    "radarr_api_key": ("arr", "radarr", "api_key"),
    "gemini_api_key": ("gemini", "api_key"),
    "openrouter_api_key": ("openrouter", "api_key"),
    "openai_compat_api_key": ("openai_compat", "api_key"),
    "anthropic_api_key": ("anthropic", "api_key"),
    "cloudflare_api_key": ("cloudflare", "api_key"),
    "deepl_api_key": ("deepl", "api_key"),
    "libretranslate_api_key": ("libretranslate", "api_key"),
    "google_api_key": ("google", "api_key"),
    "azure_api_key": ("azure", "api_key"),
    "yandex_api_key": ("yandex", "api_key"),
}


@app.route("/api/secret", methods=["POST"], endpoint="save_secret")
def save_secret():
    """Save a single API key on its own, so secrets never ride the autosave."""
    path = SECRET_FIELDS.get((request.json or {}).get("field", ""))
    if not path:
        return jsonify({"ok": False, "message": "Unknown field"}), 400
    value = ((request.json or {}).get("value") or "").strip()
    if not value:
        return jsonify({"ok": False, "message": "Empty — nothing to save"}), 400
    cfg = cfgmod.load_config()
    node = cfg
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    cfgmod.save_config(cfg)
    return jsonify({"ok": True, "message": "Saved"})


# ── Status (redacted — safe to expose) ────────────────────────────────────────
@app.route("/status")
def status():
    return jsonify({"config": cfgmod.redact(cfgmod.load_config())})


@app.route("/api/version")
def api_version():
    info = version.check()
    if not info.get("latest"):  # boot cache may predate the latest release
        info = version.check(force=True)
    return jsonify(info)


def _render_md(text):
    """Tiny, XSS-safe Markdown -> HTML for release notes (escape first)."""
    out, in_list = [], False
    for raw in (text or "").splitlines():
        line = html.escape(raw)
        if re.match(r"^#{1,6}\s+", raw):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append("<h4>" + re.sub(r"^#{1,6}\s+", "", line) + "</h4>")
        elif re.match(r"^[-*]\s+", raw):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append("<li>" + re.sub(r"^[-*]\s+", "", line) + "</li>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            if line.strip():
                out.append("<p>" + line + "</p>")
    if in_list:
        out.append("</ul>")
    h = "".join(out)
    h = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", h)
    h = re.sub(r"`(.+?)`", r"<code>\1</code>", h)
    return Markup(h)


@app.template_filter("md")
def _md_filter(text):
    return _render_md(text)


@app.route("/whats-new")
def whats_new():
    return render_template("whatsnew.html", releases=version.list_releases(),
                           current=version.__version__, active="whatsnew")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9878"))
    log.info("=" * 56)
    log.info("translAItarr2 starting on port %s", port)
    log.info("=" * 56)
    db.init_db()
    db.reset_stuck_jobs()
    worker.start()
    version.start()
    telemetry.start()
    try:
        from waitress import serve  # production WSGI server
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        log.warning("waitress not installed — falling back to Flask dev server")
        app.run(host="0.0.0.0", port=port, use_reloader=False)
