"""Sonarr / Radarr REST API clients (read-only).

translAItarr2 talks to the *arr API v3 (header ``X-Api-Key``) to enumerate the
library with proper titles and to locate the video files that may need a
translated subtitle. It never writes to Sonarr/Radarr.

A returned title is a plain dict:
    {
        "source": "sonarr" | "radarr",
        "kind":   "Episode" | "Movie",
        "id":     <arr id>,
        "title":  "Series S01E02 — Episode name"  /  "Movie (2021)",
        "sort":   "<lowercase sort key>",
        "path":   "/data/tv/Series/...mkv"   # path AS THE *ARR SEES IT
    }

Note: ``path`` is whatever Sonarr/Radarr report. If translAItarr2 mounts the
media at a different location, a path remap is applied later by the scanner.
"""
import logging

import requests

log = logging.getLogger("translaitarr2")

DEFAULT_TIMEOUT = 15


class ArrError(Exception):
    """Raised on any connection/authentication/response problem."""


class _ArrClient:
    kind = "arr"

    def __init__(self, url, api_key, timeout=DEFAULT_TIMEOUT):
        self.base = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    @property
    def configured(self):
        return bool(self.base and self.api_key)

    def _get(self, path, params=None):
        if not self.configured:
            raise ArrError(f"{self.kind} is not configured (missing URL or API key)")
        url = f"{self.base}/api/v3/{path.lstrip('/')}"
        try:
            r = requests.get(
                url, params=params,
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ArrError(f"{self.kind}: cannot reach {self.base} ({e})") from e
        if r.status_code == 401:
            raise ArrError(f"{self.kind}: unauthorized — check the API key")
        if not r.ok:
            raise ArrError(f"{self.kind}: HTTP {r.status_code} for /{path}")
        try:
            return r.json()
        except ValueError as e:
            raise ArrError(f"{self.kind}: invalid JSON from {self.base}") from e

    def test(self):
        """Return (ok: bool, message: str). Used by the setup wizard."""
        try:
            data = self._get("system/status")
            return True, f"Connected — {data.get('appName', self.kind)} {data.get('version', '')}".strip()
        except ArrError as e:
            return False, str(e)

    def list_titles(self):  # pragma: no cover - overridden
        raise NotImplementedError


class RadarrClient(_ArrClient):
    kind = "radarr"

    def list_titles(self):
        titles = []
        for m in self._get("movie"):
            if not m.get("hasFile"):
                continue
            path = (m.get("movieFile") or {}).get("path")
            if not path:
                continue
            year = m.get("year")
            label = m.get("title", "?") + (f" ({year})" if year else "")
            titles.append({
                "source": "radarr",
                "kind": "Movie",
                "id": m.get("id"),
                "title": label,
                "sort": (m.get("sortTitle") or m.get("title") or "").lower(),
                "path": path,
            })
        return titles


class SonarrClient(_ArrClient):
    kind = "sonarr"

    def list_titles(self):
        titles = []
        for s in self._get("series"):
            sid = s.get("id")
            series_title = s.get("title", "?")
            try:
                episodes = self._get("episode", params={"seriesId": sid, "includeEpisodeFile": "true"})
            except ArrError as e:
                log.warning("sonarr: skipping series %s — %s", sid, e)
                continue
            for ep in episodes:
                if not ep.get("hasFile"):
                    continue
                path = (ep.get("episodeFile") or {}).get("path")
                if not path:
                    continue
                se, en = ep.get("seasonNumber"), ep.get("episodeNumber")
                code = f"S{se:02d}E{en:02d}" if isinstance(se, int) and isinstance(en, int) else ""
                label = f"{series_title} {code}".strip()
                if ep.get("title"):
                    label += f" — {ep['title']}"
                sort = f"{series_title.lower()} {se or 0:03d}{en or 0:03d}"
                titles.append({
                    "source": "sonarr",
                    "kind": "Episode",
                    "id": ep.get("id"),
                    "title": label,
                    "sort": sort,
                    "path": path,
                })
        return titles


def clients_from_config(cfg):
    arr = cfg.get("arr", {})
    radarr = RadarrClient(arr.get("radarr", {}).get("url"), arr.get("radarr", {}).get("api_key"))
    sonarr = SonarrClient(arr.get("sonarr", {}).get("url"), arr.get("sonarr", {}).get("api_key"))
    return radarr, sonarr


def list_all_titles(cfg):
    """Combined library from both services. Returns (titles, errors).

    An unconfigured service is silently skipped; a configured one that fails
    contributes its error message to ``errors`` instead of raising.
    """
    titles, errors = [], []
    for client in clients_from_config(cfg):
        if not client.configured:
            continue
        try:
            titles.extend(client.list_titles())
        except ArrError as e:
            errors.append(str(e))
            log.warning("%s", e)
    titles.sort(key=lambda t: t["sort"])
    return titles, errors
