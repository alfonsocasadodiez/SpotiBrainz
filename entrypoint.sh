#!/bin/sh
set -eu

if [ "${1:-}" != "serve" ]; then
    exec "$@"
fi

mkdir -p "$DATA_DIR"

# cron jobs don't inherit the container environment, so snapshot it for them
export -p > /app/env.sh
chmod 600 /app/env.sh

cron
exec python /app/web.py
