"""A deliberately pedantic replica of AntennaPod's `GpodnetService`.

Written before the server it tests, and modelled on the client's source rather
than on the gpodder.net documentation, because the places the two disagree are
exactly the places other servers get wrong.

What it reproduces, and why each one matters:

* `Authorization: Basic` goes to the login endpoint and nowhere else. Everything
  after it is authenticated by the session cookie alone.
* Cookies are handled by `StrictCookieJar`, which has Java's manners rather than
  Python's.
* Mandatory fields are read the way the Java reads them — `getJSONArray` and
  `getLong` throw on an absent key — so an omitted `update_urls` raises here just
  as it crashes there.
* Episode actions upload thirty at a time.
Where it is deliberately *stricter* than the real client, and why:

* Integer fields must really be JSON numbers. Android's `org.json` coerces `"3"`
  to 3 in `getInt`, so a server returning a stringly-typed timestamp would work
  on a phone and fail here. That is the right way round: this server should never
  emit one, and a test that objects is cheaper than the habit spreading.
* A cookie's `Domain` must name the request host exactly, where Java's
  `HttpCookie.domainMatches` permits some parent forms. gpodsync never sets
  `Domain`, so the difference only appears in a test written to prove that
  setting it breaks the client.

And deliberately simpler, none of which bites a host-only `Path=/` session cookie:
cookie expiry is not modelled, cookies are keyed by name alone rather than by
name, domain and path, quoted values keep their quotes, and the redirect limit is
five rather than OkHttp's twenty.

* A redirect is followed the way `RetryAndFollowUpInterceptor` follows one, which
  is not the folklore version. `Authorization` survives a same-origin redirect
  and is dropped only when scheme, host or port change; what breaks a same-origin
  301 is that OkHttp rewrites the POST into a GET. Both roads end at "wrong
  username or password", by different mechanisms.
"""

import json
from base64 import b64encode
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from .cookies import StrictCookieJar
from .errors import MalformedResponse, SyncFailed, Unauthorised

BATCH_SIZE = 30
MAX_REDIRECTS = 5


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: str

    def first(self, name: str) -> str | None:
        lowered = name.lower()
        return next((value for key, value in self.headers if key.lower() == lowered), None)

    def all(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.headers if key.lower() == lowered]


class Transport(Protocol):
    """How a request reaches the server.

    Kept abstract so the same pedantic client can drive the Django application
    in-process for component tests and a real container over HTTP for acceptance
    tests. A fake that only worked against one of them would be trusted for
    conclusions it had not earned.
    """

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: str | None
    ) -> Response: ...


@dataclass
class FakeAntennaPod:
    base_url: str
    transport: Transport
    device_id: str = "fake-antennapod"
    jar: StrictCookieJar = field(default_factory=StrictCookieJar)
    redirects_followed: int = 0
    requests_carrying_authorization: int = 0
    credentials_dropped_by_redirect: int = 0
    methods_rewritten_by_redirect: int = 0

    # --- The seven calls ------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        token = b64encode(f"{username}:{password}".encode()).decode()
        self._request(
            "POST",
            f"/api/2/auth/{username}/login.json",
            body="",
            # Not a typo. The client really does send a media type that does not
            # exist, and a framework that negotiates content types will reject the
            # request before any application code sees it.
            content_type="plain/text; charset=utf-8",
            authorization=f"Basic {token}",
        )

    def get_devices(self, username: str) -> list[dict[str, Any]]:
        payload = self._json(self._request("GET", f"/api/2/devices/{username}.json"))
        if not isinstance(payload, list):
            raise MalformedResponse("the device list must be a JSON array")
        return [self._read_device(entry) for entry in payload]

    def configure_device(self, username: str, device_id: str, caption: str, kind: str) -> None:
        self._request(
            "POST",
            f"/api/2/devices/{username}/{device_id}.json",
            body=json.dumps({"caption": caption, "type": kind}),
            content_type="application/json",
        )

    def get_subscription_changes(
        self, username: str, device_id: str, since: int
    ) -> tuple[list[str], list[str], int]:
        payload = self._json(
            self._request("GET", f"/api/2/subscriptions/{username}/{device_id}.json?since={since}")
        )
        return (
            _require_array(payload, "add"),
            _require_array(payload, "remove"),
            _require_long(payload, "timestamp"),
        )

    def upload_subscription_changes(
        self, username: str, device_id: str, add: Sequence[str], remove: Sequence[str]
    ) -> tuple[int, list[list[str]]]:
        payload = self._json(
            self._request(
                "POST",
                f"/api/2/subscriptions/{username}/{device_id}.json",
                body=json.dumps({"add": list(add), "remove": list(remove)}),
                content_type="application/json",
            )
        )
        return _require_long(payload, "timestamp"), _require_array(payload, "update_urls")

    def get_episode_actions(self, username: str, since: int) -> tuple[list[dict], int]:
        payload = self._json(self._request("GET", f"/api/2/episodes/{username}.json?since={since}"))
        return _require_array(payload, "actions"), _require_long(payload, "timestamp")

    def upload_episode_actions(
        self, username: str, actions: Sequence[Mapping[str, Any]]
    ) -> list[tuple[int, list[list[str]]]]:
        """Upload in batches of thirty, exactly as the client does."""
        results = []
        for batch in _in_batches(actions, BATCH_SIZE):
            payload = self._json(
                self._request(
                    "POST",
                    f"/api/2/episodes/{username}.json",
                    body=json.dumps(list(batch)),
                    content_type="application/json",
                )
            )
            results.append(
                (_require_long(payload, "timestamp"), _require_array(payload, "update_urls"))
            )
        return results

    # --- Transport ------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        content_type: str | None = None,
        authorization: str | None = None,
    ) -> Response:
        url = f"{self.base_url}{path}"
        redirects = 0

        while True:
            headers: dict[str, str] = {}
            if authorization is not None:
                headers["Authorization"] = authorization
                self.requests_carrying_authorization += 1
            if content_type is not None:
                headers["Content-Type"] = content_type
            cookie = self.jar.header_for(url)
            if cookie is not None:
                headers["Cookie"] = cookie

            response = self.transport(method, url, headers, body)

            for set_cookie in response.all("Set-Cookie"):
                self.jar.store(set_cookie, url)

            if response.status in (301, 302, 303, 307, 308):
                location = response.first("Location")
                if location is None or redirects >= MAX_REDIRECTS:
                    raise SyncFailed(f"redirect from {url} with no usable Location")
                redirects += 1
                self.redirects_followed += 1
                target = location if "://" in location else f"{self.base_url}{location}"

                # Credentials survive a redirect that stays on the same origin and
                # are dropped when scheme, host or port change — an http-to-https
                # redirect being the case that bites.
                if _origin(target) != _origin(url) and authorization is not None:
                    authorization = None
                    self.credentials_dropped_by_redirect += 1

                # 301, 302 and 303 rewrite a POST into a GET. On a same-origin
                # redirect this, not the header, is what breaks login: the endpoint
                # is asked for a method it does not answer.
                if response.status in (301, 302, 303) and method == "POST":
                    method = "GET"
                    body = None
                    content_type = None
                    self.methods_rewritten_by_redirect += 1

                url = target
                continue

            if response.status == 401:
                raise Unauthorised(self._explain_401(url))
            # Exactly 200, not merely "not an error". GpodnetService.checkStatusCode
            # throws on any code other than HTTP_OK, and OkHttp has already
            # followed redirects by the time it runs. A 201 or a 204 — both
            # natural choices for these POST endpoints — would pass a lenient
            # suite and fail on every real device.
            if response.status != 200:
                raise SyncFailed(
                    f"{method} {url} returned {response.status}. The client requires "
                    f"exactly 200; checkStatusCode rejects everything else."
                )
            return response

    def _explain_401(self, url: str) -> str:
        if len(self.jar) == 0:
            return (
                f"401 from {url} and this client holds no cookie at all. Either "
                f"login set none, or the jar refused the one it was sent."
            )
        withheld = self.jar.withheld_because_insecure(url)
        if withheld:
            return (
                f"401 from {url}. The jar holds {withheld} but will not send them "
                f"over plain http because they are marked Secure."
            )
        return f"401 from {url} with a cookie sent."

    def _json(self, response: Response) -> Any:
        try:
            return json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise MalformedResponse(f"response body is not JSON: {response.body[:120]!r}") from exc

    def _read_device(self, entry: Any) -> dict[str, Any]:
        if not isinstance(entry, Mapping):
            raise MalformedResponse("each device must be a JSON object")
        for key in ("id", "caption", "type"):
            if key not in entry:
                raise MalformedResponse(f"a device is missing {key!r}")
        subscriptions = entry.get("subscriptions")
        if not isinstance(subscriptions, int) or isinstance(subscriptions, bool):
            raise MalformedResponse("a device's 'subscriptions' must be a whole number")
        return dict(entry)


def _require_array(payload: Any, key: str) -> list:
    """Read a field the Java reads with `getJSONArray`, which throws when absent."""
    if not isinstance(payload, Mapping) or key not in payload:
        raise MalformedResponse(
            f"{key!r} is missing. The client reads it with getJSONArray, which "
            f"throws rather than defaulting, so this is a crash on the phone."
        )
    value = payload[key]
    if not isinstance(value, list):
        raise MalformedResponse(f"{key!r} must be a JSON array, got {type(value).__name__}")
    return value


def _require_long(payload: Any, key: str) -> int:
    """Read a field the Java reads with `getLong`, which throws when absent."""
    if not isinstance(payload, Mapping) or key not in payload:
        raise MalformedResponse(
            f"{key!r} is missing. The client reads it with getLong, which throws "
            f"rather than defaulting."
        )
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedResponse(f"{key!r} must be a whole number, got {value!r}")
    return value


DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme
    # An implicit port and its explicit equivalent are the same origin. Comparing
    # them raw made a redirect that merely spells out `:443` look like a scheme
    # change, and drop credentials OkHttp would have kept.
    port = parts.port if parts.port is not None else DEFAULT_PORTS.get(scheme)
    return scheme, (parts.hostname or ""), port


def _in_batches(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
