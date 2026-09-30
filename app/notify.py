"""Notifications — the same idea as Sonarr/Radarr's Settings → Connect.

Each notifier is a service + its fields + which events it wants. Delivery goes
through Apprise (one library, 100+ services), so the common services get a
friendly form here and everything else is reachable through a raw Apprise URL.
The "webhook" service is ours: a plain JSON POST for scripts and home automation.

Stored in config as ``notifications: [{id, service, name, fields, events}]``.
Secret fields are masked for the browser (see ``redact``) and kept on save when
the browser sends the mask back unchanged.
"""
import copy
import logging
import secrets
import threading
from urllib.parse import quote, urlencode, urlsplit

import requests

log = logging.getLogger("translaitarr2")

MASK = "********"
TIMEOUT = 15

# Events a notifier can subscribe to. Order = order in the UI.
EVENTS = [
    ("translated", "Subtitle translated"),
    ("failed", "Job failed"),
    ("verify_issues", "Verification found issues"),
    ("exhausted", "All providers out of quota"),
]
DEFAULT_EVENTS = {"translated": False, "failed": True, "verify_issues": False, "exhausted": True}

# service -> label, fields [(name, label, kind, hint)], kind: text | secret | select:<a>,<b>
SERVICES = {
    "pushover": ("Pushover", [
        ("user_key", "User key", "secret", ""),
        ("app_token", "Application API token", "secret", "Create an application at pushover.net"),
        ("devices", "Devices", "text", "Optional, comma-separated. Empty = all devices"),
        ("priority", "Priority", "select:normal,low,moderate,high,emergency", ""),
    ]),
    "discord": ("Discord", [
        ("webhook_url", "Webhook URL", "secret", "Channel → Edit → Integrations → Webhooks"),
    ]),
    "telegram": ("Telegram", [
        ("bot_token", "Bot token", "secret", "From @BotFather"),
        ("chat_id", "Chat ID", "text", "Your user id, a group id (starts with -) or @channel"),
    ]),
    "ntfy": ("ntfy", [
        ("server", "Server URL", "text", "https://ntfy.sh or your own server"),
        ("topic", "Topic", "text", ""),
        ("token", "Access token", "secret", "Optional, for protected topics"),
    ]),
    "gotify": ("Gotify", [
        ("server", "Server URL", "text", "e.g. https://gotify.example.com"),
        ("app_token", "Application token", "secret", ""),
    ]),
    "slack": ("Slack", [
        ("webhook_url", "Webhook URL", "secret", "https://hooks.slack.com/services/…"),
    ]),
    "webhook": ("Webhook (JSON)", [
        ("url", "URL", "text", "Receives a JSON POST: {event, title, message, file, app}"),
        ("auth_header", "Authorization header", "secret", "Optional, sent as-is"),
    ]),
    "apprise": ("Other (Apprise URL)", [
        ("url", "Apprise URL", "secret", "Email, Matrix, Signal, Pushbullet, Home Assistant and 100+ more"),
    ]),
}


def secret_fields(service):
    return {n for n, _l, kind, _h in SERVICES.get(service, ("", []))[1] if kind == "secret"}


def schema():
    """Service definitions for the Settings page."""
    return [{"id": sid, "label": label,
             "fields": [{"name": n, "label": l, "kind": k, "hint": h} for n, l, k, h in fields]}
            for sid, (label, fields) in SERVICES.items()]


def redact(notifiers):
    out = [n for n in copy.deepcopy(notifiers or []) if isinstance(n, dict)]
    for n in out:
        for f in secret_fields(n.get("service")):
            if n.get("fields", {}).get(f):
                n["fields"][f] = MASK
    return out


def merge_incoming(incoming, existing):
    """Clean a notifier posted by the browser. A masked secret keeps the stored
    value; unknown services/fields are dropped. Returns (notifier, error)."""
    service = incoming.get("service")
    if service not in SERVICES:
        return None, "Unknown service"
    nid = str(incoming.get("id") or "") or secrets.token_hex(4)
    fields = {}
    for name, label, kind, _h in SERVICES[service][1]:
        v = str((incoming.get("fields") or {}).get(name) or "").strip()
        if kind == "secret" and v == MASK:
            v = ((existing or {}).get("fields") or {}).get(name, "")
        if kind.startswith("select:") and v not in kind[7:].split(","):
            v = kind[7:].split(",")[0]
        fields[name] = v
    events = {e: bool((incoming.get("events") or {}).get(e)) for e, _ in EVENTS}
    name = str(incoming.get("name") or "").strip()[:60] or SERVICES[service][0]
    n = {"id": nid, "service": service, "name": name, "fields": fields, "events": events}
    try:
        _target(n)
    except ValueError as e:
        return None, str(e)
    return n, None


def _server_url(raw, plain, secure, what):
    """https://host:port/path → ('ntfys', 'host:port/path'). Scheme optional (https)."""
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    u = urlsplit(raw)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"{what}: enter the server URL, e.g. https://example.com")
    return (secure if u.scheme == "https" else plain), (u.netloc + u.path).rstrip("/")


def _target(n):
    """Build the delivery target: ('apprise', url) or ('webhook', url). Raises
    ValueError with a user-facing message when a required field is missing."""
    s, f = n["service"], n.get("fields", {})

    def need(*names):
        for name in names:
            if not f.get(name):
                label = next(l for fn, l, *_ in SERVICES[s][1] if fn == name)
                raise ValueError(f"{label} is required")

    if s == "pushover":
        need("user_key", "app_token")
        devices = "/".join(quote(d.strip(), safe="") for d in f.get("devices", "").split(",") if d.strip())
        url = f"pover://{quote(f['user_key'], safe='')}@{quote(f['app_token'], safe='')}"
        url += ("/" + devices) if devices else ""
        return "apprise", url + "?" + urlencode({"priority": f.get("priority") or "normal"})
    if s in ("discord", "slack"):
        need("webhook_url")
        return "apprise", f["webhook_url"]          # Apprise understands the native webhook URLs
    if s == "telegram":
        need("bot_token", "chat_id")
        return "apprise", f"tgram://{f['bot_token']}/{quote(f['chat_id'], safe='@-')}"
    if s == "ntfy":
        need("server", "topic")
        scheme, base = _server_url(f["server"], "ntfy", "ntfys", "Server URL")
        url = f"{scheme}://{base}/{quote(f['topic'], safe='')}"
        return "apprise", url + ("?" + urlencode({"token": f["token"]}) if f.get("token") else "")
    if s == "gotify":
        need("server", "app_token")
        scheme, base = _server_url(f["server"], "gotify", "gotifys", "Server URL")
        return "apprise", f"{scheme}://{base}/{quote(f['app_token'], safe='')}"
    if s == "webhook":
        need("url")
        if not f["url"].startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return "webhook", f["url"]
    need("url")
    return "apprise", f["url"]


def _deliver(n, title, message, payload):
    """Send one notification. Returns (ok, message)."""
    try:
        kind, url = _target(n)
    except ValueError as e:
        return False, str(e)
    if kind == "webhook":
        headers = {"User-Agent": "translAItarr2"}
        if n["fields"].get("auth_header"):
            headers["Authorization"] = n["fields"]["auth_header"]
        try:
            r = requests.post(url, json={**payload, "title": title, "message": message,
                                         "app": "translAItarr2"}, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            return False, f"Can't reach the webhook: {e.__class__.__name__}"
        return (True, "Sent") if r.ok else (False, f"Webhook answered HTTP {r.status_code}")

    import apprise  # imported lazily: only needed once a notifier exists

    ap = apprise.Apprise()
    if not ap.add(url):
        return False, "Apprise doesn't recognise this URL"
    # Apprise logs failures itself (at WARNING, with the service's reply); capture them
    # so the Test button can show why instead of a bare "failed".
    with apprise.LogCapture(level=apprise.logging.WARNING) as output:
        ok = ap.notify(title=title, body=message)
        detail = output.getvalue().strip().splitlines()
    if ok:
        return True, "Sent"
    return False, (detail[-1][:300] if detail else "Delivery failed")


def send(cfg, event, title, message, file=None):
    """Fire ``event`` to every notifier subscribed to it. Runs in a background
    thread so a slow service never holds up the worker. Never raises."""
    try:
        targets = [n for n in cfg.get("notifications") or []
                   if isinstance(n, dict) and (n.get("events") or {}).get(event)]
    except Exception:  # noqa: BLE001 - a hand-edited config must not break the worker
        log.warning("Notifications: the 'notifications' setting is malformed — skipped")
        return
    if not targets:
        return
    payload = {"event": event, "file": file}

    def run():
        for n in targets:
            try:
                ok, msg = _deliver(n, title, message, payload)
            except Exception as e:  # noqa: BLE001 - notifications are best-effort
                ok, msg = False, e.__class__.__name__
            if not ok:
                log.warning("Notification '%s' failed: %s", n.get("name"), msg)

    threading.Thread(target=run, name="notify", daemon=True).start()


def test(n):
    """Send a test message through one notifier synchronously (Settings button)."""
    return _deliver(n, "translAItarr2 test",
                    "Notifications work. You'll get a message here for the events you ticked.",
                    {"event": "test", "file": None})
