FROM python:3.12-slim

# ffmpeg       — extract embedded subtitle streams from MKV/MP4
# mkvtoolnix   — container muxing helpers used during extraction
# tesseract    — OCR engine for PGS (Blu-ray bitmap) subtitles, + language packs
# gosu         — drop from root to PUID/PGID at runtime
# The placeholder mkvinfo alternative only gives mkvtoolnix's preinst something to
# remove; without it a fresh install prints "update-alternatives: error".
ARG DEBIAN_FRONTEND=noninteractive
RUN update-alternatives --install /usr/bin/mkvinfo mkvinfo /bin/true 1 >/dev/null \
    && apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
        gosu \
        curl \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-fra \
        tesseract-ocr-deu \
        tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=9878

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 9878
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-u", "main.py"]
