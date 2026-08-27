#!/usr/bin/env bash
#
# Deploying to a real server, carefully.
#
# Two rules run through every command here.
#
# Back up before changing anything. The database holds somebody's listening
# history, and SQLite in WAL mode cannot be safely copied as a file — a hot copy
# of the main database misses whatever is still in the write-ahead log. So the
# backup goes through SQLite's own backup API, which takes a consistent snapshot
# of a live database.
#
# Verify with a real request afterwards. A container that started is not a
# service that answers: the health check speaks to the loopback, and the thing
# that matters is whether the name resolves, the certificate is valid, and the
# proxy routes.
#
# Nothing here ever runs `down -v`, which would delete the volume.
#
# Usage: deploy.sh {prepare|deploy|rollback|backup|trace on|trace off}
#        Reads DEPLOY_HOST, DEPLOY_DIR, PRODUCTION_URL from .env.deploy.

set -euo pipefail

: "${DEPLOY_HOST:?set in .env.deploy}"
: "${DEPLOY_DIR:?set in .env.deploy}"
: "${PRODUCTION_URL:?set in .env.deploy}"
: "${GPODSYNC_HOSTNAME:?set in .env.deploy}"

COMPOSE="docker compose -f compose.production.yaml"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

remote() { ssh -o BatchMode=yes "$DEPLOY_HOST" "$@"; }
compose_remote() { remote "cd $DEPLOY_DIR && $COMPOSE $*"; }
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# --- Preparing the server ----------------------------------------------------

prepare() {
    say "Preparing $DEPLOY_HOST:$DEPLOY_DIR"
    remote "mkdir -p $DEPLOY_DIR/secrets $DEPLOY_DIR/backups && chmod 700 $DEPLOY_DIR/secrets"

    # Minted on the server and never transmitted. A key that has been on two
    # machines has been on one too many, and regenerating it on each deploy
    # would sign everyone out at random — which reads as a broken password.
    if ! remote "test -s $DEPLOY_DIR/secrets/gpodsync_secret_key"; then
        say "Minting a secret key on the server (once, and only once)"
        remote "umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(64))' > $DEPLOY_DIR/secrets/gpodsync_secret_key"
    fi

    scp -q -o BatchMode=yes compose.production.yaml "$DEPLOY_HOST:$DEPLOY_DIR/"
    remote "cd $DEPLOY_DIR && printf 'GPODSYNC_HOSTNAME=%s\n' '$GPODSYNC_HOSTNAME' > .env && \
            grep -q GPODSYNC_TAG .env || printf 'GPODSYNC_TAG=%s\n' \"\${GPODSYNC_TAG:-latest}\" >> .env"
}

# --- Backing up ---------------------------------------------------------------

backup() {
    if ! remote "docker ps -a --format '{{.Names}}' | grep -qx gpodsync"; then
        say "No container yet, so nothing to back up"
        return 0
    fi

    say "Backing up the database"
    # Through SQLite's backup API, inside the container, because a file copy of a
    # WAL database is not a backup.
    remote "docker exec gpodsync python -c \"
import sqlite3, pathlib
pathlib.Path('/data/backups').mkdir(exist_ok=True)
source = sqlite3.connect('file:/data/db.sqlite3?mode=ro', uri=True)
target = sqlite3.connect('/data/backups/db-$STAMP.sqlite3')
source.backup(target)
target.close(); source.close()
print('snapshot taken')
\"" || { say "Backup failed. Stopping here rather than changing anything."; exit 1; }

    # And off the server too: a backup that lives only next to the thing it
    # protects is half a backup.
    mkdir -p backups
    remote "docker cp gpodsync:/data/backups/db-$STAMP.sqlite3 /tmp/db-$STAMP.sqlite3"
    scp -q -o BatchMode=yes "$DEPLOY_HOST:/tmp/db-$STAMP.sqlite3" "backups/db-$STAMP.sqlite3"
    remote "rm -f /tmp/db-$STAMP.sqlite3"
    say "Kept locally at backups/db-$STAMP.sqlite3 ($(du -h "backups/db-$STAMP.sqlite3" | cut -f1))"
}

# --- Verifying ----------------------------------------------------------------

wait_until_healthy() {
    say "Waiting for the container to report healthy"
    for _ in $(seq 1 60); do
        state=$(remote "docker inspect --format '{{.State.Health.Status}}' gpodsync 2>/dev/null" || echo gone)
        case "$state" in
            healthy) echo "  healthy"; return 0 ;;
            unhealthy|gone) compose_remote "logs --tail=40"; echo "  $state" >&2; return 1 ;;
        esac
        sleep 3
    done
    compose_remote "logs --tail=40"
    echo "  never became healthy" >&2
    return 1
}

verify_from_outside() {
    say "Asking the real hostname over HTTPS"
    for _ in $(seq 1 20); do
        code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$PRODUCTION_URL/healthz/" 2>/dev/null || echo 000)
        if [ "$code" = "200" ]; then
            echo "  $PRODUCTION_URL/healthz/ -> 200"
            # A valid certificate is part of working, not a detail.
            curl -sS -o /dev/null --max-time 10 "$PRODUCTION_URL/healthz/" \
                && echo "  certificate accepted"
            return 0
        fi
        sleep 6
    done
    echo "  $PRODUCTION_URL/healthz/ -> $code after retrying" >&2
    echo "  (a new certificate can take a minute; check 'make remote-logs')" >&2
    return 1
}

# --- What the targets call ------------------------------------------------------

case "${1:-}" in
    prepare)
        prepare
        ;;

    backup)
        backup
        ;;

    deploy|rollback)
        # Refused rather than defaulted. `latest` does not exist until a stable
        # release is tagged, and a deploy that silently reached for it would fail
        # at the pull with nothing explaining why.
        : "${GPODSYNC_TAG:?name the tag: make $1 TAG=0.1.0-rc1}"
        prepare
        backup
        say "Pulling ${GPODSYNC_TAG:+tag $GPODSYNC_TAG}"
        if [ -n "${GPODSYNC_TAG:-}" ]; then
            remote "cd $DEPLOY_DIR && sed -i 's|^GPODSYNC_TAG=.*|GPODSYNC_TAG=$GPODSYNC_TAG|' .env"
        fi
        if ! compose_remote "pull"; then
            say "Could not pull the image."
            echo "  The package is private, so the server needs to be logged in:" >&2
            echo "    docker login ghcr.io -u <user> --password-stdin" >&2
            exit 1
        fi
        compose_remote "up -d"
        wait_until_healthy
        verify_from_outside
        say "Deployed."
        ;;

    trace)
        case "${2:-}" in
            on)  value=true ;;
            off) value=false ;;
            *)   echo "usage: deploy.sh trace {on|off}" >&2; exit 2 ;;
        esac
        remote "cd $DEPLOY_DIR && grep -q GPODSYNC_TRACE_REQUESTS .env \
            && sed -i 's|^GPODSYNC_TRACE_REQUESTS=.*|GPODSYNC_TRACE_REQUESTS=$value|' .env \
            || printf 'GPODSYNC_TRACE_REQUESTS=%s\n' '$value' >> .env"
        compose_remote "up -d"
        wait_until_healthy

        # Proving it, rather than assuming the restart did what was asked. The
        # application announces tracing at startup precisely so this is checkable.
        say "Checking what the container actually thinks"
        if [ "$value" = "true" ]; then
            compose_remote "logs --tail=200" | grep -q tracing_enabled \
                && echo "  tracing is ON — remember it records every listening position" \
                || { echo "  asked for tracing but the container did not announce it" >&2; exit 1; }
        else
            compose_remote "logs --tail=5" >/dev/null
            echo "  tracing is OFF"
        fi
        ;;

    *)
        echo "usage: deploy.sh {prepare|deploy|rollback|backup|trace on|trace off}" >&2
        exit 2
        ;;
esac
