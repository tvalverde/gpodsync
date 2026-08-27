"""A cookie jar with Java's manners, not Python's.

AntennaPod uses `JavaNetCookieJar` over a `CookieManager` set to
`CookiePolicy.ACCEPT_ORIGINAL_SERVER`, and the session cookie is the only thing
authenticating every request after login. Two of its attributes decide whether
the client will ever send it back, and getting either wrong produces the same
symptom: login succeeds, everything afterwards is 401, and the user is told their
password is wrong.

Python's `http.cookiejar` is more forgiving than this, which is exactly why the
tests must not use it. A jar that accepts what the phone rejects would let the
server ship a bug that only appears on a real device.

Deliberate simplifications, all in the strict direction, documented so that a
surprising test failure can be traced here rather than to the server:

* A `Domain` attribute is accepted only when it names the request host exactly.
  Java's `HttpCookie.domainMatches` permits some parent-domain forms; this jar
  does not. gpodsync never sets `Domain`, so the difference only shows up in a
  test written to prove that setting it breaks the client.
* Expiry is not modelled. Nothing in these tests waits.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class Cookie:
    name: str
    value: str
    path: str
    secure: bool


class RejectedCookie(Exception):
    """A cookie the client discarded, with the reason it was discarded.

    The real client discards silently. Raising instead is the entire point of this
    replica: a silent discard is precisely the failure that is hard to diagnose on
    a phone, so the test suite refuses to let it be silent.
    """


class StrictCookieJar:
    def __init__(self) -> None:
        self._cookies: dict[str, Cookie] = {}

    def store(self, set_cookie: str, request_url: str) -> None:
        """Apply one `Set-Cookie` header, or raise explaining the refusal."""
        parts = set_cookie.split(";")
        name, equals, value = parts[0].strip().partition("=")
        if not equals:
            raise RejectedCookie(f"{set_cookie!r} is not a name=value pair")

        attributes = {}
        for attribute in parts[1:]:
            key, _, attribute_value = attribute.strip().partition("=")
            attributes[key.lower()] = attribute_value

        host = urlsplit(request_url).hostname or ""
        domain = attributes.get("domain")
        if domain is not None and domain.lstrip(".").lower() != host.lower():
            raise RejectedCookie(
                f"Domain={domain!r} does not name the request host {host!r} exactly, "
                f"so ACCEPT_ORIGINAL_SERVER discards this cookie. Emit a host-only "
                f"cookie: no Domain attribute at all."
            )

        self._cookies[name] = Cookie(
            name=name,
            value=value,
            path=attributes.get("path", "/"),
            secure="secure" in attributes,
        )

    def header_for(self, request_url: str) -> str | None:
        """The `Cookie` header this jar would send, if any."""
        parts = urlsplit(request_url)
        over_https = parts.scheme == "https"
        path = parts.path or "/"

        sendable = [
            cookie
            for cookie in self._cookies.values()
            if path.startswith(cookie.path) and (over_https or not cookie.secure)
        ]
        if not sendable:
            return None
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in sendable)

    def withheld_because_insecure(self, request_url: str) -> list[str]:
        """Cookies held back only because this request is not over https.

        Java will not return a `Secure` cookie over plain http. On an HTTPS
        deployment that is what you want; on a LAN served over http it makes the
        server unusable, and the symptom is indistinguishable from a wrong
        password. Tests use this to say so out loud.
        """
        if urlsplit(request_url).scheme == "https":
            return []
        return [cookie.name for cookie in self._cookies.values() if cookie.secure]

    def __len__(self) -> int:
        return len(self._cookies)
