#!/usr/bin/env bash
set -e

# Run as an unprivileged user matching the host (PUID/PGID), like the *arr images.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

groupmod -o -g "$PGID" appuser 2>/dev/null || groupadd -o -g "$PGID" appuser
id -u appuser >/dev/null 2>&1 || useradd -o -u "$PUID" -g "$PGID" -d /config -s /bin/bash appuser
usermod -o -u "$PUID" -g "$PGID" appuser 2>/dev/null || true

mkdir -p /config
chown -R "$PUID:$PGID" /config 2>/dev/null || true

exec gosu "$PUID:$PGID" "$@"
