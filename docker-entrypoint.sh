#!/bin/sh
#
# Prepare the data directory, become the application user, migrate, and hand over.
#
# Two ways of running this both have to work, and they need opposite things.
#
# Unprivileged (`user: "10001:10001"`, every capability dropped) is the
# recommended shape and needs nothing at all: the image ships /data owned by that
# user, so a fresh named volume inherits it.
#
# As root, usually because a host directory is bind-mounted with someone else's
# ownership, this fixes the ownership and then drops privileges *before*
# migrating — so every file the application creates belongs to the user that will
# reopen it next start, rather than leaving a root-owned database it cannot read.

set -eu

DATA_DIR="${GPODSYNC_DATA_DIR:-/data}"
APP_USER=gpodsync
APP_UID=10001

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"

    # Only when it is not already right: chowning a large volume on every start
    # is a slow way to say nothing. Failure is not fatal — with CAP_CHOWN dropped
    # this cannot work, and it does not need to if the ownership is already good.
    if [ "$(stat -c %u "$DATA_DIR")" != "$APP_UID" ]; then
        chown -R "$APP_USER:$APP_USER" "$DATA_DIR" 2>/dev/null || true
    fi

    if ! su-exec "$APP_USER" test -w "$DATA_DIR" 2>/dev/null; then
        echo "gpodsync: $DATA_DIR is not writable by the application user, and this" >&2
        echo "          container cannot change that — CAP_CHOWN is unavailable." >&2
        echo "          Either give the volume to uid $APP_UID, or run the container" >&2
        echo "          as that user directly with --user $APP_UID:$APP_UID." >&2
        exit 1
    fi

    exec su-exec "$APP_USER" "$0" "$@"
fi

if [ ! -w "$DATA_DIR" ]; then
    echo "gpodsync: $DATA_DIR is not writable by uid $(id -u)." >&2
    echo "          Give the volume to that user, or let the container start as" >&2
    echo "          root so it can do so itself." >&2
    exit 1
fi

# Idempotent: the ordinary case is that there is nothing to apply. Quiet, because
# the migration list is progress output rather than a record of anything, and
# every other line this container writes is structured.
python manage.py migrate --noinput --verbosity 0

# Does nothing unless GPODSYNC_BOOTSTRAP_USER is set, and nothing on any start
# after the first.
python manage.py bootstrap_account

# The client logs in at the start of every sync, so session rows accumulate
# several times a day forever. There is no cron in this image to clear them
# anywhere else.
python manage.py clearsessions

exec "$@"
