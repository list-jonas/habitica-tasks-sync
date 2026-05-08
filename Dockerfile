# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System packages:
#   tini             clean signal forwarding (zombie reaping)
#   ca-certificates  HTTPS to Habitica/Google
#   tzdata           respect TZ env var for timestamp formatting
#   gosu             drop root → app at runtime (lighter than su/sudo,
#                    exec-replaces so signals reach Python)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates tzdata gosu \
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

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV HABITICA_SYNC_CONFIG=/etc/habitica-tasks-sync/config.yaml \
    APP_USER=app

VOLUME ["/data", "/tokens", "/etc/habitica-tasks-sync"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD python -m habitica_tasks_sync.healthcheck

# Stay root for the entrypoint so it can fix bind-mount ownership; it
# drops to `app` via gosu before exec'ing the daemon.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "habitica_tasks_sync"]
