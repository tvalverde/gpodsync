# Pinned by digest as well as tag. A tag can be moved; a digest cannot, and this
# image is published for other people to run.
FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc AS builder

# --require-hashes is the point of the lock file. Version pins alone say which
# release was intended; hashes say the bytes are the ones that were reviewed.
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 # pip has no job in a runtime image, and leaving it there hands a compromised
 # process a package installer it did not have to bring itself.
 && /opt/venv/bin/pip uninstall -y pip setuptools 2>/dev/null || true


FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

LABEL org.opencontainers.image.title="gpodsync" \
      org.opencontainers.image.description="A self-hosted podcast sync server that AntennaPod actually works with" \
      org.opencontainers.image.source="https://github.com/tvalverde/gpodsync" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

# su-exec drops privileges without the process-supervision baggage of gosu or the
# signal-swallowing of `su`, so uvicorn stays PID 1's direct child and receives
# its own SIGTERM.
# Pinned like everything else. It is the one package installed outside the
# hash-verified lock file, so it should at least not float.
RUN apk add --no-cache "su-exec=0.3-r0" \
 && addgroup -g 10001 -S gpodsync \
 && adduser -u 10001 -S -G gpodsync -h /app gpodsync

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=gpodsync.settings \
    GPODSYNC_DATA_DIR=/data

COPY --from=builder /opt/venv /opt/venv

# Created here, owned here. A fresh named volume mounted over this path inherits
# the directory's ownership from the image, so the hardened configuration — an
# unprivileged user with every capability dropped — works on a first start
# without anyone needing CAP_CHOWN to fix it up.
RUN mkdir -p /data && chown gpodsync:gpodsync /data

WORKDIR /app
COPY gpodsync/ ./gpodsync/
COPY manage.py ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

# No curl, no wget: a health check is not a reason to carry an HTTP client that a
# compromised container could also use. Against 127.0.0.1 so it tests the
# application rather than the proxy in front of it.
# The port is read from the environment rather than baked in: the server honours
# GPODSYNC_PORT, and a probe hardcoded to 8000 would report a container that works
# perfectly as unhealthy forever.
# A generous start period, because the slowest machine that runs this is the
# point of shipping arm64 at all. Emulated, first boot takes about seventy-six
# seconds — migrations, then the account bootstrap, then session cleanup, then
# uvicorn — and a Raspberry Pi on an SD card is not much quicker. With fifteen
# seconds, Docker marked a container unhealthy while it was still starting
# perfectly well, three failed probes into a boot that had not finished.
#
# It costs nothing to be generous: failures during the start period do not count
# towards the retries, and the first successful probe ends it immediately.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 CMD \
    python -c "import os,sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:%s/healthz/' % os.environ.get('GPODSYNC_PORT','8000'), timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "gpodsync.server"]
