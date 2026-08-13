FROM python:3.12-slim

ENV TZ=America/Chicago
ENV DEBIAN_FRONTEND=noninteractive

# Chrome + the OS libs Selenium/Chrome need to actually run (headless still needs most of these)
# tzdata provides the zoneinfo database so TZ above actually takes effect
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        gnupg \
        ca-certificates \
        tzdata \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libnspr4 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xdg-utils \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY uscis_watcher.py summary.py ./

# config.json and the output/ directory are provided via volume mounts at
# runtime (see docker-compose.yml) - nothing sensitive gets baked into the image
VOLUME ["/app/output"]

ENV DOCKER_CONTAINER=1

ENTRYPOINT ["uv", "run", "uscis_watcher.py"]
# Default CMD (no flags) - loop/interval/jitter/log-server settings come from
# config.json's "schedule"/"log_server" sections. docker-compose.yml overrides
# this with just ["-v"] for verbose logging.
CMD []

EXPOSE 8080