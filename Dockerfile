# One image for every service; docker-compose picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime for YouTube (signature / challenge solving); without
# one it warns "No supported JavaScript runtime" and many formats are missing.
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml ./
COPY core ./core
COPY gateway ./gateway
COPY workers ./workers
RUN pip install .

RUN useradd -m bot && mkdir -p /tmp/jobs && chown bot:bot /tmp/jobs
USER bot
# deno caches its own files; give it a writable home.
ENV DENO_DIR=/home/bot/.cache/deno

CMD ["python", "-m", "gateway.main"]
