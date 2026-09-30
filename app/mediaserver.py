"""Tell a media server (Jellyfin / Emby / Plex) that a subtitle appeared or disappeared.

Without this the new .srt only shows up after the server's next library scan.

- Jellyfin and Emby take the same "a file changed" notice: POST /Library/Media/Updated
  with the video's path. The server then re-reads that one item (including its
  external subtitles) after its own short debounce — about a minute on Jellyfin.
  Only the auth header differs: Jellyfin 12 accepts nothing but
  ``Authorization: MediaBrowser Token="…"``, Emby wants ``X-Emby-Token``.
- Plex gets a partial scan of just the video's folder: find the library whose
  folder contains the file, then GET /library/sections/{key}/refresh?path=<folder>.
  New or removed sidecars are picked up within seconds.
"""
import logging
import os

import requests

log = logging.getLogger("translaitarr2")

KINDS = {"jellyfin": "Jellyfin", "emby": "Emby", "plex": "Plex"}
TIMEOUT = 15


def _headers(kind, key):
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if kind == "plex":
        h["X-Plex-Token"] = key
    elif kind == "emby":
        h["X-Emby-Token"] = key
    else:
        h["Authorization"] = f'MediaBrowser Token="{key}"'
    return h


def server_path(path, ms):
    """Map translAItarr2's local path to the path the media server sees.
    Rules are {"from": local_prefix, "to": server_prefix}; identity when none match."""
    for rule in ms.get("remap", []):
        frm, to = rule.get("from"), rule.get("to")
        if frm and path.startswith(frm):
            return to + path[len(frm):]
    return path


def configured(cfg):
    ms = cfg.get("mediaserver") or {}
    return ms.get("kind") in KINDS and bool(ms.get("url")) and bool(ms.get("api_key"))


def test(kind, url, key):
    """Return (ok, message) for the Settings "Test connection" button."""
    if kind not in KINDS:
        return False, "Pick a server"
    if not url or not key:
        return False, "URL and API key are required"
    if kind == "plex":
        return _plex_test(url, key)
    try:
        r = requests.get(url.rstrip("/") + "/System/Info", headers=_headers(kind, key), timeout=TIMEOUT)
    except requests.RequestException as e:
        return False, f"Can't reach {url}: {e.__class__.__name__}"
    if r.status_code in (401, 403):
        return False, "The server rejected the API key"
    if not r.ok:
        return False, f"HTTP {r.status_code}"
    try:
        info = r.json()
    except ValueError:
        return False, "Not a Jellyfin/Emby server (no JSON from /System/Info)"
    return True, f"Connected to {info.get('ServerName') or KINDS[kind]} ({info.get('Version', '?')})"


def _plex_get(url, key, path, **params):
    r = requests.get(url.rstrip("/") + path, headers=_headers("plex", key), params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("MediaContainer") or {}


def _plex_test(url, key):
    try:
        server = _plex_get(url, key, "/")
        sections = _plex_get(url, key, "/library/sections").get("Directory") or []
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            return False, "Plex rejected the token"
        return False, f"HTTP {e.response.status_code if e.response is not None else '?'}"
    except requests.RequestException as e:
        return False, f"Can't reach {url}: {e.__class__.__name__}"
    except ValueError:
        return False, "Not a Plex server (no JSON reply)"
    return True, (f"Connected to {server.get('friendlyName') or 'Plex'} ({server.get('version', '?')}), "
                  f"{len(sections)} librar{'y' if len(sections) == 1 else 'ies'}")


def _plex_section_for(sections, path):
    """Key of the library whose folder holds ``path`` (longest match wins)."""
    best, best_len = None, -1
    for d in sections:
        for loc in d.get("Location") or []:
            root = (loc.get("path") or "").rstrip("/")
            if root and (path == root or path.startswith(root + "/")) and len(root) > best_len:
                best, best_len = d.get("key"), len(root)
    return best


def _plex_refresh(ms, target):
    folder = os.path.dirname(target)
    try:
        sections = _plex_get(ms["url"], ms["api_key"], "/library/sections").get("Directory") or []
        key = _plex_section_for(sections, target)
        if key is None:
            log.warning("Plex refresh skipped: no Plex library contains %s "
                        "(check the media server path mapping)", folder)
            return False
        r = requests.get(ms["url"].rstrip("/") + f"/library/sections/{key}/refresh",
                         headers=_headers("plex", ms["api_key"]), params={"path": folder}, timeout=TIMEOUT)
        r.raise_for_status()
    except (requests.RequestException, ValueError) as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        log.warning("Plex refresh failed: %s", f"HTTP {code}" if code else e.__class__.__name__)
        return False
    log.info("Plex asked to rescan %s", folder)
    return True


def refresh(cfg, video_path):
    """Ask the media server to re-read one video (and so its sidecar subtitles).
    Never raises — a media-server hiccup must not fail the translation job."""
    try:
        return _refresh(cfg, video_path)
    except Exception as e:  # noqa: BLE001 - best-effort, the subtitle is already written
        log.warning("Media server refresh failed: %s", e)
        return False


def _refresh(cfg, video_path):
    if not configured(cfg):
        return False
    ms = cfg["mediaserver"]
    target = server_path(video_path, ms)
    if ms["kind"] == "plex":
        return _plex_refresh(ms, target)
    body = {"Updates": [{"Path": target, "UpdateType": "Modified"}]}
    try:
        r = requests.post(ms["url"].rstrip("/") + "/Library/Media/Updated",
                          headers=_headers(ms["kind"], ms["api_key"]), json=body, timeout=TIMEOUT)
    except requests.RequestException as e:
        log.warning("%s refresh failed: %s", KINDS[ms["kind"]], e.__class__.__name__)
        return False
    if not r.ok:
        log.warning("%s refresh failed: HTTP %s", KINDS[ms["kind"]], r.status_code)
        return False
    log.info("%s asked to refresh %s", KINDS[ms["kind"]], target)
    return True
