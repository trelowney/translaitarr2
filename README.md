# translAItarr2

**AI subtitle translation for your media library — with a web UI and native Sonarr/Radarr integration.**

translAItarr2 watches your Sonarr/Radarr library, finds video files that don't yet
have a subtitle in your language, and translates one for them using Google Gemini.
Unlike most subtitle-translation tools, it doesn't need a ready-made `.srt` lying
around — it can **extract embedded subtitles from the video** and even **OCR
Blu-ray (PGS) bitmap subtitles** into text before translating.

> Status: **early development.** This README is written alongside the code and will
> grow as features land. Expect rough edges.

---

## Why another one?

Tools like [Bazarr](https://www.bazarr.media/) and
[Lingarr](https://github.com/lingarr-translate/lingarr) translate subtitle files
that already exist on disk. They can't help when the only English subtitle is
**embedded inside the MKV** or is a **PGS bitmap** (common on Blu-ray rips).
translAItarr2 handles those cases:

- ✅ Translates existing sidecar `.srt`
- ✅ Extracts and translates **embedded** text subtitles
- ✅ **OCRs PGS** (Blu-ray bitmap) subtitles to text, then translates
- ✅ Skips files that **already have** your target language (audio, embedded sub, or sidecar)
- ✅ Picks the best source language by your configured priority (e.g. English first, then French, German, Spanish…)

## Features

- **Web UI (dark, minimal)** — Library, Queue, and Settings.
- **Native Sonarr/Radarr integration** via their REST API — the library shows your
  real series / episode / movie titles, not raw file paths. **No webhook needed.**
- **First-run setup wizard** — connect Sonarr/Radarr, add your Gemini key, choose
  languages, and optionally set a password. Nothing to hand-edit.
- **Automation** — optional periodic scan that translates anything new without your
  target language; or trigger translations manually per-title.
- **Quality options** — SDH/caption stripping, surrounding-line context for better
  translations, and output sanity validation.
- **Privacy & safety first** — no telemetry; secrets stay in your local config
  volume; runs as a non-root user.

## Quick start

```bash
git clone https://github.com/REPLACE_ME/translaitarr2.git
cd translaitarr2
cp docker-compose.example.yaml docker-compose.yaml
# edit volumes (your media path) and PUID/PGID, then:
docker compose up -d
```

Open `http://<host>:9878` and follow the setup wizard.

> **Note:** translAItarr2 must be able to reach your Sonarr/Radarr API and read your
> media files. If you run it inside your existing *arr Docker network you can use
> service names like `http://sonarr:8989`; otherwise use the host's IP and port.

## Updating

Because the image is published to a registry, updating is the same as for any
*arr app — pull the newer image and recreate the container (your config in the
`/config` volume is untouched):

```bash
docker compose pull && docker compose up -d
```

**Automatic updates:** add [Watchtower](https://github.com/containrrr/watchtower)
to your stack (most *arr users already run it) and it will pull new releases and
recreate translAItarr2 for you — exactly like it does for Sonarr/Radarr:

```yaml
  watchtower:
    image: nickfedor/watchtower:latest
    environment:
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_POLL_INTERVAL=3600
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped
```

Pin to a specific version (e.g. `image: ghcr.io/REPLACE_ME/translaitarr2:1.2.0`)
if you'd rather update deliberately instead of tracking `:latest`.

## Configuration

All settings are managed in the web UI (Settings tab) and persisted to
`config/config.json` in your mounted config volume. `config.example.json` documents
every field. Highlights:

| Area        | What it controls                                                        |
|-------------|-------------------------------------------------------------------------|
| Sonarr/Radarr | API URL + key for each (used to list the library with proper titles). |
| Gemini      | API key and an ordered list of models (tried in order on rate-limit).   |
| Languages   | Source-language priority order, and your single target language.        |
| SDH         | Strip captions/sound effects/speaker labels before translating.         |
| Limits      | Daily per-model and total request caps; max titles per automation run.  |
| Automation  | On/off and scan interval.                                               |
| Translation | Timeout, retries, context window, optional translator credit line.      |
| Validation  | Min/max cue length and duration sanity checks on the output.            |

### Recommended Gemini models & batch size

translAItarr2 sends subtitles to Gemini in **batches** (N cues per request) and tries
your models top-to-bottom, falling back to the next one when a model is rate-limited.
On the **free tier each model has its own small daily request quota** (roughly ~20
requests/day per model, reset at midnight US-Pacific), so a bigger batch means fewer
requests and more subtitles per day — but too big risks truncated or lower-quality
output. Sensible starting points:

| Model (example)              | Tier | Suggested batch (cues/request) | Notes                                    |
|------------------------------|------|--------------------------------|------------------------------------------|
| `gemini-3-flash` / `2.0-flash` | free | **~200**                       | Strong; handles large batches well       |
| `*-flash-lite`               | free | **~150**                       | Faster/cheaper, slightly smaller batches |
| `*-pro`                      | paid | ~250                           | Best quality; needs a paid key/quota     |

Tune the global **`batch_size`** in Settings, and override per model via
`gemini.model_batch` in config. As a feel: a typical 700–900-cue film translates in
~4–6 requests at batch 150–200. Google changes quotas often — check your current
limits in Google AI Studio.

### Secrets

Your API keys live only in the mounted `config/` volume and are **never** part of
the image or the repo. You can also supply any secret via an environment variable,
or via a Docker secret using the `*_FILE` convention
(e.g. `GEMINI_API_KEY_FILE=/run/secrets/gemini_key`). Keys are write-only in the UI
and redacted from logs.

## Default port

`9878` (configurable via the `PORT` env). Chosen to avoid clashing with common *arr
services (Sonarr 8989, Radarr 7878, Lidarr 8686, Readarr 8787, Prowlarr 9696,
Bazarr 6767) so it can coexist on the same host.

## How it decides what to translate

For each downloaded title Sonarr/Radarr reports, translAItarr2 inspects the actual
file and **skips** it if it already has the target language as audio, an embedded
subtitle, or a sidecar `.srt`. Otherwise it selects the best available source
subtitle (by your priority order), extracting or OCR-ing it if needed, and queues a
translation. A re-translation is triggered automatically when a video file is
replaced by a newer (upgraded) release.

## Requirements for OCR

PGS OCR uses Tesseract (CPU-only) via [`pgsrip`](https://github.com/ratoaq2/pgsrip).
Language data for English, French, German, and Spanish ships in the image.

## License

[GPL-3.0](LICENSE).

## Acknowledgements

Inspired by the *arr ecosystem and by [Lingarr](https://github.com/lingarr-translate/lingarr).
OCR via [pgsrip](https://github.com/ratoaq2/pgsrip) + [Tesseract](https://github.com/tesseract-ocr/tesseract).
