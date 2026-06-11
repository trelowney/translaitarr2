"""Anonymous active-instance counter (opt-out — on by default).

Sends ONLY a random instance id (generated once, stored in the config volume) and
the app version, at most once a day, to a small Cloudflare Worker that just counts
distinct ids seen in the last ~30 days. Nothing else is sent — no file paths, API
keys, library data, settings, or anything internal — and the receiver does not
store IP addresses. Users can turn it off in Settings; a disabled instance never
even generates an id. See the README's Privacy section.
"""
import logging
import os
import threading
import time
import uuid

import requests

import config as cfgmod
import version

log = logging.getLogger("translaitarr2")

# The counter endpoint — a tiny Cloudflare Worker (source in telemetry-worker/). Fixed in
# code on purpose: it's the project's shared counter, not user-configurable (otherwise the
# count couldn't be trusted). It only accepts {id, version} and returns a count — nothing
# about the account can be reached, read or changed through this URL. Opt out with the
# Settings toggle or TELEMETRY=off.
ENDPOINT = "https://translaitarr2-telemetry.trelowney.workers.dev/ping"
PING_INTERVAL = 24 * 3600  # once a day
_started = False


def _instance_id():
    """Stored anonymous id, generated + persisted on first use (only when enabled)."""
    cfg = cfgmod.load_config()
    iid = cfg.get("telemetry", {}).get("instance_id", "")
    if not iid:
        iid = uuid.uuid4().hex
        cfg.setdefault("telemetry", {})["instance_id"] = iid
        try:
            cfgmod.save_config(cfg)
        except OSError:
            pass
    return iid


def _disabled_by_env():
    return os.environ.get("TELEMETRY", "").strip().lower() in ("off", "0", "false", "no")


def _ping():
    if _disabled_by_env():
        return
    cfg = cfgmod.load_config()
    if not cfg.get("telemetry", {}).get("enabled", True):
        return  # opted out — generate nothing, send nothing
    requests.post(ENDPOINT, json={"id": _instance_id(), "version": version.__version__}, timeout=8)


def start():
    """Start the once-a-day anonymous ping (idempotent). Never raises."""
    global _started
    if _started:
        return
    _started = True

    def _loop():
        while True:
            try:
                _ping()
            except Exception:  # noqa: BLE001 - telemetry must never affect the app
                pass
            time.sleep(PING_INTERVAL)

    threading.Thread(target=_loop, name="telemetry", daemon=True).start()
