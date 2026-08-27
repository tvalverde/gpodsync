"""Driving the Django application in-process with the pedantic client.

The same replica that will face a real container over HTTP also faces the
application here, so a divergence between the two layers is a bug in the server
rather than in two different test doubles.

The Django test client keeps its own cookie jar, which is deliberately defeated:
it is forgiving in exactly the ways `StrictCookieJar` is not, and letting it
carry the session would hide the failure this whole project exists to fix. Its
jar is emptied after every call, leaving the strict one as the only thing that
can produce a `Cookie` header.
"""

from collections.abc import Mapping
from urllib.parse import urlsplit

from django.test import Client

from .client import Response


class DjangoTestClientTransport:
    def __init__(self, client: Client) -> None:
        self.client = client

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: str | None
    ) -> Response:
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")

        response = self.client.generic(
            method,
            path,
            data=(body or "").encode(),
            content_type=headers.get("Content-Type", "application/octet-stream"),
            secure=parts.scheme == "https",
            headers={
                name.lower(): value
                for name, value in headers.items()
                if name.lower() != "content-type"
            },
        )

        emitted = [("Set-Cookie", morsel.OutputString()) for morsel in response.cookies.values()]
        # Whatever the server just set is now the strict jar's problem, not the
        # test client's.
        self.client.cookies.clear()

        return Response(
            status=response.status_code,
            headers=tuple(response.items()) + tuple(emitted),
            body=response.content.decode("utf-8", errors="replace"),
        )
