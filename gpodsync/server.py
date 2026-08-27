"""Starting the server with the application's own logging, not uvicorn's.

The uvicorn command-line installs a logging configuration of its own, which
writes plain text and bypasses both the JSON formatter and the health-probe
filter. That would make "every line is one JSON object" false the moment anybody
looked at a real log, and would restore the thirty-second heartbeat the filter
exists to remove.

So logging is configured first, by Django, and uvicorn is told to leave it alone.
"""

import os

import django


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "gpodsync.settings")

    # Before uvicorn writes its first line, so root already has the JSON handler.
    django.setup()

    import uvicorn
    from django.conf import settings

    uvicorn.run(
        "gpodsync.asgi:application",
        host=os.environ.get("GPODSYNC_HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("GPODSYNC_PORT", "8000")),
        # None means "do not touch logging", which is the whole point.
        log_config=None,
        # The request log is the middleware's job: it redacts, it carries the
        # correlation id, and it drops passing health probes.
        access_log=False,
        # One less thing announcing its version to the internet.
        server_header=False,
        # asgi.py is a bare get_asgi_application() with no lifespan support, so
        # letting uvicorn probe for it only prints a warning on every boot.
        lifespan="off",
        proxy_headers=settings.BEHIND_PROXY,
        forwarded_allow_ips=os.environ.get("GPODSYNC_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
