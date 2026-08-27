"""Talking to a real container over real HTTP.

The same replica that drives the application in process drives it here, so a
difference between the two layers is a difference in the server rather than
between two test doubles.

`requests` is only the socket. Its cookie jar and its redirect following are both
switched off: the first is far more forgiving than Java's and would hide the
failure this project exists to fix, and the second would follow a redirect the
way Python does rather than the way OkHttp does.
"""

from collections.abc import Mapping

import requests

from .client import Response

TIMEOUT_SECONDS = 15


class HttpTransport:
    def __init__(self) -> None:
        self.session = requests.Session()

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: str | None
    ) -> Response:
        response = self.session.request(
            method,
            url,
            headers=dict(headers),
            data=(body or "").encode() if body is not None else None,
            allow_redirects=False,
            timeout=TIMEOUT_SECONDS,
        )

        # Several Set-Cookie headers collapse into one comma-joined value in the
        # ordinary mapping, which is ambiguous — a cookie's Expires attribute
        # contains a comma. The raw headers keep them apart.
        emitted = response.raw.headers.getlist("Set-Cookie") if response.raw is not None else []
        others = [
            (name, value)
            for name, value in response.headers.items()
            if name.lower() != "set-cookie"
        ]

        # Whatever the server just set is the strict jar's business, not this
        # session's.
        self.session.cookies.clear()

        return Response(
            status=response.status_code,
            headers=tuple(others) + tuple(("Set-Cookie", value) for value in emitted),
            body=response.text,
        )
