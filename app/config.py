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
    ("openrouter", "api_key"),
    ("openai_compat", "api_key"),
    ("anthropic", "api_key"),
    ("cloudflare", "api_key"),
    ("deepl", "api_key"),
    ("libretranslate", "api_key"),
    ("google", "api_key"),
    ("azure", "api_key"),
    ("yandex", "api_key"),
    ("arr", "sonarr", "api_key"),
    ("arr", "radarr", "api_key"),
    ("auth", "password_hash"),
)

DEFAULTS = {
    "onboarding_completed": False,
    "auth": {"enabled": False, "password_hash": "", "session_days": 30},
    "arr": {
        "sonarr": {"url": "http://sonarr:8989", "api_key": ""},
        "radarr": {"url": "http://radarr:7878", "api_key": ""},
    },
    "gemini": {
        "api_key": "",
        # Full flash-family fallback order (newest -> oldest). On the free tier
        # each model has its own daily quota, so the worker simply skips any that
        # return 429/5xx and tries the next. Paid-only pro models are omitted.
        "models": [
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-flash-latest",
            "gemini-2.5-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
        # Per-model batch override (cues per request). Models not listed fall back
        # to translation.batch_size — that's where the smaller "lite" models land.
        "model_batch": {
            "gemini-3.5-flash": 200,
            "gemini-3-flash-preview": 200,
            "gemini-2.5-flash": 200,
            "gemini-flash-latest": 200,
            "gemini-2.0-flash": 200,
        },
        # Per-model daily request cap. Models not listed fall back to
        # limits.max_daily_per_model. The worker skips a model once its count
        # for the day reaches this.
        "model_daily_limit": {},
    },
    # OpenRouter — OpenAI-compatible gateway to many models. Empty by default;
    # models are fetched from its API and chosen in the UI.
    "openrouter": {
        "api_key": "",
        "models": [],
        "model_batch": {},
        "model_daily_limit": {},
    },
    # Generic OpenAI-compatible endpoint — covers local servers (Ollama, LM Studio,
    # vLLM) and hosted ones (Groq, DeepSeek, OpenAI, Mistral…). Just point base_url
    # at the server's /v1 and (optionally) supply a key. Empty by default.
    "openai_compat": {
        "api_key": "",
        "base_url": "",
        "models": [],
        "model_batch": {},
        "model_daily_limit": {},
    },
    # Anthropic Claude — native Messages API (its own request shape, not OpenAI).
    # base_url defaults to the official endpoint; override via env for a gateway.
    "anthropic": {
        "api_key": "",
        "base_url": "",
        "models": [],
        "model_batch": {},
        "model_daily_limit": {},
    },
    # Cloudflare Workers AI — OpenAI-style inference addressed by account id (the
    # base URL is derived from it). Free tier ≈ 10k neurons/day. api_key = a
    # Workers-AI API token.
    "cloudflare": {
        "api_key": "",
        "account_id": "",
        "models": [],
        "model_batch": {},
        "model_daily_limit": {},
    },
    # DeepL — dedicated machine translation (not an LLM). Just an API key; the
    # free vs paid endpoint is auto-detected from the key (free keys end in ':fx').
    "deepl": {"api_key": ""},
    # LibreTranslate — open-source/self-hostable MT. Point base_url at the server
    # (e.g. http://host:5000); api_key is optional (only public/paid instances need one).
    "libretranslate": {"api_key": "", "base_url": ""},
    # Google Cloud Translation (v2) — just an API key.
    "google": {"api_key": ""},
    # Microsoft / Azure Translator — key + the resource region (e.g. westeurope).
    "azure": {"api_key": "", "region": ""},
    # Yandex Translate — API key + the cloud folder id.
    "yandex": {"api_key": "", "folder_id": ""},
    # Cloudflare m2m100 reuses the credentials from the "cloudflare" block above
    # (no separate config) — it's the same account, just the translation model.
    # AI provider priority: primary first, then secondary, then tertiary (each a
    # provider name or "none"). Cross-provider fallback.
    "ai": {"primary": "gemini", "secondary": "none", "tertiary": "none"},
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
    # Anonymous active-instance counter. Opt-OUT (on by default). Sends ONLY a random
    # instance id (generated once, stored here) + the app version, once a day. No paths,
    # keys, library data or anything internal. See telemetry.py / README.
    "telemetry": {"enabled": True, "instance_id": ""},
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
        # 'video' = translate the subtitle inside the video (embedded/PGS);
        # 'sidecar' = prefer an external source .srt next to it, then fall back.
        "source_preference": "video",
        # Back-translation verification: sample the result back to the source
        # language and flag dubious jobs. Costs one extra request per job.
        "verify": False,
        "verify_samples": 8,
        # When a release is upgraded and the new file already carries the target
        # language (embedded audio or subtitle), delete the now-stale sidecar we
        # translated earlier. Only ever removes subtitles translAItarr2 created.
        "cleanup_superseded": True,
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
# Source languages offered as clickable priority (canonical ISO-639-2 codes that
# media.py can match against subtitle tracks), in default priority order.
SOURCE_LANGUAGES = [
    ("eng", "English"), ("fra", "French"), ("deu", "German"), ("spa", "Spanish"),
    ("ita", "Italian"), ("por", "Portuguese"), ("pol", "Polish"), ("nld", "Dutch"),
    ("rus", "Russian"),
]

# Target languages offered in the dropdown (code used for the sidecar filename).
TARGET_LANGUAGES = [
    ("cs", "Czech"), ("sk", "Slovak"), ("en", "English"), ("de", "German"),
    ("fr", "French"), ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"),
    ("pl", "Polish"), ("nl", "Dutch"), ("ru", "Russian"), ("uk", "Ukrainian"),
    ("hu", "Hungarian"), ("ro", "Romanian"), ("hr", "Croatian"), ("sr", "Serbian"),
    ("bg", "Bulgarian"), ("el", "Greek"), ("tr", "Turkish"), ("sv", "Swedish"),
    ("da", "Danish"), ("fi", "Finnish"), ("no", "Norwegian"), ("ja", "Japanese"),
    ("ko", "Korean"), ("zh", "Chinese"), ("ar", "Arabic"),
]


def target_name_for(code):
    return dict(TARGET_LANGUAGES).get(code, code.upper())


ENV_OVERRIDES = {
    "GEMINI_API_KEY": ("gemini", "api_key"),
    "OPENROUTER_API_KEY": ("openrouter", "api_key"),
    "OPENAI_COMPAT_API_KEY": ("openai_compat", "api_key"),
    "OPENAI_COMPAT_BASE_URL": ("openai_compat", "base_url"),
    "ANTHROPIC_API_KEY": ("anthropic", "api_key"),
    "ANTHROPIC_BASE_URL": ("anthropic", "base_url"),
    "CLOUDFLARE_API_TOKEN": ("cloudflare", "api_key"),
    "CLOUDFLARE_ACCOUNT_ID": ("cloudflare", "account_id"),
    "DEEPL_API_KEY": ("deepl", "api_key"),
    "LIBRETRANSLATE_URL": ("libretranslate", "base_url"),
    "LIBRETRANSLATE_API_KEY": ("libretranslate", "api_key"),
    "GOOGLE_TRANSLATE_API_KEY": ("google", "api_key"),
    "AZURE_TRANSLATOR_KEY": ("azure", "api_key"),
    "AZURE_TRANSLATOR_REGION": ("azure", "region"),
    "YANDEX_API_KEY": ("yandex", "api_key"),
    "YANDEX_FOLDER_ID": ("yandex", "folder_id"),
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
