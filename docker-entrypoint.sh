#!/bin/sh
# Container entrypoint.
#
# Bind-mounted host directories arrive owned by whichever UID created
# them (often root from `docker compose up` itself). The non-root `app`
# user inside the container can't write SQLite WAL files or refresh
# tokens to a root-owned mount, so we fix ownership at startup.
#
# We're invoked as root via tini so we can chown, then drop privileges
# to `app`. If we're already non-root (user supplied --user), we skip
# the chown and exec directly — chown would fail with EPERM and the
# permissions are presumably already correct.

set -eu

APP_USER="${APP_USER:-app}"
APP_UID="$(id -u "$APP_USER")"
APP_GID="$(id -g "$APP_USER")"

if [ "$(id -u)" = "0" ]; then
    for d in /data /tokens; do
        if [ -d "$d" ]; then
            # Only chown when needed; skipping no-ops keeps log noise down
            # and avoids touching files we shouldn't (e.g. read-only dirs
            # mounted intentionally for shared client secrets).
            current_uid="$(stat -c '%u' "$d" 2>/dev/null || stat -f '%u' "$d")"
            if [ "$current_uid" != "$APP_UID" ]; then
                chown -R "$APP_UID:$APP_GID" "$d" 2>/dev/null || true
            fi
        fi
    done
    # `gosu` is tiny and exec-replaces; avoid extra PID layer that
    # would intercept signals between tini and Python.
    exec gosu "$APP_USER" "$@"
fi

exec "$@"
