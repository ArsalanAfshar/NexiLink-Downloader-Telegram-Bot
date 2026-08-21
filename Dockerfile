# ---------------------------------------------------------------------------
# NexiLink Telegram Downloader Bot
# Python 3.11 + FFmpeg/ffprobe + Deno (JS runtime + PO-Token provider) + git
# Optimized for Railway's Free Plan: small image, fast build, low RAM/CPU.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1

WORKDIR /app

# FFmpeg (incl. ffprobe, used for video dimension probing and for muxing
# separate audio/video streams / converting to mp3), plus git (needed by
# the PO Token provider) and Deno (a real JS runtime YouTube's anti-bot
# challenge requires for most non-trivial formats).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
        git \
        unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

ENV PATH="/usr/local/bin:${PATH}"

# Best-effort PO Token provider setup (bgutil-ytdlp-pot-provider) running
# in "script" mode via Deno — no extra server process needed, which keeps
# this a single-container deployment. If this step fails (e.g. network
# hiccup during build) the image build still succeeds; the bot falls back
# to alternative YouTube player clients that don't require a token.
ARG BGUTIL_POT_VERSION=1.3.1
RUN git clone --depth 1 --single-branch --branch "${BGUTIL_POT_VERSION}" \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
        /root/bgutil-ytdlp-pot-provider \
    && cd /root/bgutil-ytdlp-pot-provider/server \
    && (deno install --allow-scripts=npm:canvas --frozen \
        && echo "bgutil PO Token provider (deno) ready." \
        || echo "Warning: PO Token provider setup failed; continuing with fallback clients.")

# Python dependencies (separate layer for better Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && yt-dlp --version

# Application code
COPY . .

# Ephemeral temp download directory (Railway Free Plan disks are
# ephemeral; this is also wiped on every startup by main.py)
RUN mkdir -p /tmp/downloads

ENV DOWNLOAD_PATH=/tmp/downloads

# Runs both the main bot and the NexiLink Manager Bot in the same process.
CMD ["python", "main.py"]
