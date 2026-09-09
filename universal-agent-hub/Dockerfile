# ---------------------------------------------------------------------------
# Universal Agent Hub — container image
#
#   docker build -t universal-agent-hub:latest .
#   docker run --rm -it --env-file .env -v "$PWD:/app/workspace" universal-agent-hub:latest
#
# The image installs the package with the `system` + `search` extras (accurate
# metrics and higher-quality web search). Playwright is intentionally NOT part
# of the default image: it adds ~400 MB of browser binaries. Build with
# `--build-arg INSTALL_BROWSER=1` if you need the browser tools.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS base

ARG INSTALL_BROWSER=0
ARG VERSION=1.0.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8

# curl: healthchecks · procps: `ps` for the terminal tool · git: real-world repos
# · ca-certificates + openssl: TLS for every outbound call
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openssl \
        procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer cache survives source-only changes
COPY pyproject.toml requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install "psutil>=5.9" "ddgs>=9.0"

COPY . .
RUN pip install --no-deps -e .

# Optional browser automation layer (kept out of the default image on purpose)
RUN if [ "$INSTALL_BROWSER" = "1" ]; then \
        pip install "playwright>=1.40" \
        && playwright install --with-deps chromium \
        && rm -rf /root/.cache/pip ; \
    fi

# Run as an unprivileged user: the agent can execute commands, so it must not be root
RUN useradd --create-home --shell /bin/bash --uid 10001 agent \
    && mkdir -p /app/workspace /app/logs \
    && chown -R agent:agent /app
USER agent
WORKDIR /app/workspace

ENV HOME=/home/agent \
    LOG_FILE=/app/logs/agent.log \
    ALLOWED_DIRECTORIES=/app/workspace \
    PROJECT_ROOT=/app/workspace

LABEL org.opencontainers.image.title="universal-agent-hub" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.description="Modular, safety-guarded AI agent with full system access" \
      org.opencontainers.image.licenses="MIT"

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import src.cli, src.agent; print('ok')" || exit 1

ENTRYPOINT ["agent-hub"]
CMD ["--help"]
