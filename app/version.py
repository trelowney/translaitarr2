"""Self-update awareness: compare the running version against the latest GitHub
release and expose the result (plus changelog) to the UI.

The network fetch runs on a background thread and is cached, so rendering a page
never blocks on GitHub — templates read :func:`info`, which is cache-only.
"""
import logging
import re
import threading
import time

import requests

log = logging.getLogger("translaitarr2")

__version__ = "0.1.1"
# Upstream repo to check for releases. Forks can point this elsewhere later.
REPO = "trelowney/translaitarr2"
CHECK_INTERVAL = 6 * 3600  # seconds

_cache = {"checked": 0.0, "latest": None, "url": None, "changelog": None}
_started = False


def _parse(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def info():
    """Cache-only snapshot for templates (never hits the network)."""
    latest = _cache["latest"]
    return {
        "current": __version__,
        "latest": latest,
        "update_available": bool(latest and _parse(latest) > _parse(__version__)),
        "url": _cache["url"] or f"https://github.com/{REPO}/releases",
        "changelog": _cache["changelog"],
    }


def check(force=False):
    """Refresh the cache from the GitHub Releases API. Safe to call often."""
    now = time.time()
    if not force and now - _cache["checked"] < CHECK_INTERVAL:
        return info()
    _cache["checked"] = now
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"}, timeout=8,
        )
        if r.status_code == 200:
            d = r.json()
            _cache["latest"] = d.get("tag_name")
            _cache["url"] = d.get("html_url")
            _cache["changelog"] = d.get("body")
        elif r.status_code == 404:
            log.info("Version check: no releases published yet")
    except requests.RequestException as e:
        log.warning("Version check failed: %s", e)
    return info()


_releases = {"checked": 0.0, "items": []}


def list_releases(force=False):
    """All published releases (newest first), cached. For the What's New page."""
    now = time.time()
    if not force and _releases["items"] and now - _releases["checked"] < CHECK_INTERVAL:
        return _releases["items"]
    _releases["checked"] = now
    try:
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/releases",
            headers={"Accept": "application/vnd.github+json"},
            params={"per_page": 30}, timeout=8,
        )
        if r.status_code == 200:
            _releases["items"] = [
                {"tag": x.get("tag_name"), "name": x.get("name"),
                 "body": x.get("body") or "", "date": (x.get("published_at") or "")[:10],
                 "url": x.get("html_url")}
                for x in r.json() if not x.get("draft")
            ]
    except requests.RequestException as e:
        log.warning("Release list fetch failed: %s", e)
    return _releases["items"]


def start():
    """Start the periodic background version check (idempotent)."""
    global _started
    if _started:
        return
    _started = True

    def _loop():
        while True:
            check(force=True)
            time.sleep(CHECK_INTERVAL)

    threading.Thread(target=_loop, name="version", daemon=True).start()
