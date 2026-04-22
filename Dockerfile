# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System packages: tini for clean signal handling, ca-certificates for HTTPS,
# tzdata so RFC3339 conversions respect the host timezone if requested.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first to maximize layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps .

# Non-root user. Created with explicit UID/GID so volumes mounted from the
# host are writable when the host user matches.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid ${APP_GID} app \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data /etc/habitica-tasks-sync /tokens \
    && chown -R app:app /data /etc/habitica-tasks-sync /tokens

USER app

ENV HABITICA_SYNC_CONFIG=/etc/habitica-tasks-sync/config.yaml

VOLUME ["/data", "/tokens", "/etc/habitica-tasks-sync"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD python -m habitica_tasks_sync.healthcheck

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "habitica_tasks_sync"]
