"""Configuration loading/saving for translAItarr2.

Config lives in the mounted volume at /config/config.json. It is created by the
first-run setup wizard, never shipped with the image. Secrets may also come from
environment variables (or *_FILE Docker secrets), which always win over the file.
"""
import copy
import json
import os

from werkzeug.security import check_password_hash, generate_password_hash

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_FILE = os.path.join(CONFIG_DIR, "queue.db")
LOG_FILE = os.path.join(CONFIG_DIR, "translaitarr2.log")

# Field paths that must never be sent to the browser or written to logs.
SECRET_PATHS = (
    ("gemini", "api_key"),
    ("arr", "sonarr", "api_key"),
    ("arr", "radarr", "api_key"),
    ("auth", "password_hash"),
)

DEFAULTS = {
    "onboarding_completed": False,
    "auth": {"enabled": False, "password_hash": ""},
    "arr": {
        "sonarr": {"url": "http://sonarr:8989", "api_key": ""},
        "radarr": {"url": "http://radarr:7878", "api_key": ""},
    },
    "gemini": {
        "api_key": "",
        "models": ["gemini-3-flash-preview", "gemini-2.0-flash", "gemini-2.0-flash-lite"],
        # Per-model batch override (cues per request). Models not listed here use
        # translation.batch_size. Lite models get a slightly smaller default batch.
        "model_batch": {
            "gemini-3-flash-preview": 200,
            "gemini-2.0-flash": 200,
            "gemini-2.0-flash-lite": 150,
        },
    },
    "languages": {
        "source_priority": ["eng", "fra", "deu", "spa"],
        "target": {"name": "Czech", "code": "cs"},
    },
    # Map *arr-reported paths to translAItarr2's local mount, if they differ.
    # Each rule: {"from": "/movies", "to": "/data/movies"}. Empty = identity.
    "paths": {"remap": []},
    "sdh": {"brackets": True, "parens": True, "music": True, "speaker": True, "uppercase": False},
    "limits": {"max_daily_per_model": 18, "max_daily_total": 120, "max_per_run": 10},
    "automation": {"enabled": False, "scan_interval_minutes": 30, "rpd_reset_tz": "America/Los_Angeles"},
    "translation": {
        "api_timeout": 1200,
        "max_output_tokens": 65536,
        "max_retries": 3,
        "retry_delay": 5,
        "context_enabled": True,
        "context_before": 2,
        "context_after": 2,
        "add_translator_credit": False,
        "batch_size": 150,
    },
    "validation": {
        "enabled": True,
        "min_chars": 1,
        "max_chars": 200,
        "min_duration_ms": 100,
        "max_duration_s": 15,
    },
}

# Environment variable -> config path. Each also supports a *_FILE variant
# pointing at a file (Docker secret) whose contents are used instead.
ENV_OVERRIDES = {
    "GEMINI_API_KEY": ("gemini", "api_key"),
    "SONARR_URL": ("arr", "sonarr", "url"),
    "SONARR_API_KEY": ("arr", "sonarr", "api_key"),
    "RADARR_URL": ("arr", "radarr", "url"),
    "RADARR_API_KEY": ("arr", "radarr", "api_key"),
}


def _deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _set_path(cfg, path, value):
    node = cfg
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _get_path(cfg, path):
    node = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _env_value(name):
    """Return env value, honoring the *_FILE Docker-secret convention."""
    file_var = os.environ.get(f"{name}_FILE")
    if file_var and os.path.exists(file_var):
        with open(file_var) as f:
            return f.read().strip()
    return os.environ.get(name)


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            cfg = _deep_merge(cfg, json.load(f))
    # Environment / secrets override the file.
    for name, path in ENV_OVERRIDES.items():
        val = _env_value(name)
        if val:
            _set_path(cfg, path, val)
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def config_exists():
    return os.path.exists(CONFIG_FILE)


def redact(cfg):
    """Deep copy with every secret replaced by a placeholder, for UI/logs."""
    safe = copy.deepcopy(cfg)
    for path in SECRET_PATHS:
        if _get_path(safe, path):
            _set_path(safe, path, "********")
    return safe


def hash_password(plain):
    return generate_password_hash(plain)


def verify_password(cfg, plain):
    h = _get_path(cfg, ("auth", "password_hash"))
    return bool(h) and check_password_hash(h, plain)
