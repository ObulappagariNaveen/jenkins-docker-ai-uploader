#!/bin/sh
set -e

# /data may be an older volume created when the app ran as root.
# Give it to the app user once, then drop root before starting the app.
if [ "$(id -u)" = "0" ]; then
    DATA_PATH="${DATA_DIR:-/data}"
    mkdir -p "$DATA_PATH"
    chown -R app:app "$DATA_PATH"
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi

exec "$@"
