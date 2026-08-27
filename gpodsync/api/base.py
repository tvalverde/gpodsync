"""The shared behaviour of every endpoint.

Authentication here is unusual, and the shape is forced by the client rather than
chosen. AntennaPod sends `Authorization: Basic` to login and nothing afterwards,
so the session cookie carries every later request; it sends no CSRF token, so the
API cannot require one; and it must never be redirected. What is left is a narrow
path, and the compensating controls below are what keep it safe.
"""

import json
import logging
from typing import Any, cast

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import AbstractBaseUser
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from gpodsync.domain.addresses import client_address
from gpodsync.domain.credentials import decode_basic_auth

# One body, one status, one content type, for every way of failing to
# authenticate: no credentials, an unknown user, a wrong password, a dead
# session, or a URL naming somebody else. A 403 for the last of those would
# confirm that the account exists, and the username is in the path of every
# request.
logger = logging.getLogger("gpodsync.auth")

# The client reads a cursor with getLong, a signed 64-bit value.
MAX_CURSOR = 2**63 - 1

UNAUTHORISED_BODY = b'{"error": "unauthorised"}'

# Deliberately absent: WWW-Authenticate. The client does not need it, and sending
# it makes a browser that stumbles onto the API pop a credential prompt.


def axes_client_ip(request: HttpRequest) -> str | None:
    """What the lockout is keyed on, wired in as AXES_CLIENT_IP_CALLABLE.

    django-axes would otherwise reach for django-ipware and, finding it absent,
    fall back to REMOTE_ADDR while silently ignoring every proxy setting — which
    behind a reverse proxy collapses the key to (username, the proxy) and makes
    the lockout lock out the owner. Deciding this here costs one small function
    and keeps a dependency out of a published image.
    """
    return client_address(
        remote_addr=request.META.get("REMOTE_ADDR"),
        forwarded_for=request.headers.get("X-Forwarded-For"),
        trusted_hops=settings.TRUSTED_PROXY_HOPS,
    )


def authenticated_as(user: AbstractBaseUser | None, username: str | None) -> bool:
    """Whether these credentials may act as the account named in the path.

    One place, because both the login endpoint and every other endpoint need the
    rule and they were drifting apart: the path names an account, the credentials
    name an account, and a disagreement is not a different kind of failure.
    """
    return user is not None and (username is None or user.get_username() == username)


def unauthorised() -> HttpResponse:
    return HttpResponse(UNAUTHORISED_BODY, status=401, content_type="application/json")


def refuse(reason: str, **details: Any) -> HttpResponse:
    """One identical 401 for the client, one specific reason for the log.

    Every refusal goes through here so the asymmetry is structural rather than
    remembered: what reaches the network cannot distinguish these cases, and what
    reaches the log always does.
    """
    logger.info("refused: %s", reason, extra={"event": "auth_refused", "reason": reason, **details})
    return unauthorised()


def bad_request(reason: str) -> JsonResponse:
    return JsonResponse({"error": reason}, status=400)


def forbidden(reason: str) -> JsonResponse:
    return JsonResponse({"error": reason}, status=403)


def locked_out(
    request: HttpRequest, credentials: dict | None = None, *args: Any, **kwargs: Any
) -> JsonResponse:
    """What axes returns once an address has failed too often.

    429 rather than 401, and it does not matter to the client: AntennaPod treats
    every failure as one retryable sync error. It matters to whoever is reading
    the logs.
    """
    return JsonResponse({"error": "too many failed attempts"}, status=429)


class GpodderApiView(View):
    """Base for the seven endpoints.

    Subclasses implement `get` and `post` and return an `HttpResponse`.
    """

    # Login is the exception: it authenticates rather than requiring an existing
    # session, and its URL names the user being authenticated.
    requires_authentication = True

    # Set by dispatch once authentication has succeeded. Subclasses use this
    # rather than request.user, whose declared type still admits AnonymousUser
    # and would have every endpoint restating a guarantee dispatch already made.
    account: AbstractBaseUser

    @method_decorator(csrf_exempt)
    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        cross_site = self._reject_cross_site(request)
        if cross_site is not None:
            return cross_site

        if self.requires_authentication:
            user = self._authenticate(request)
            if user is None:
                return refuse("no_credentials")
            # A disagreement here is the same 401 as every other failure, so that
            # probing paths reveals nothing about which accounts exist.
            if not authenticated_as(user, kwargs.get("username")):
                return refuse("username_mismatch", path_username=kwargs.get("username"))
            # Set for the handler's benefit; the type is narrower on the
            # request than what authenticate() promises to return.
            request.user = cast(Any, user)
            self.account = user

        return super().dispatch(request, *args, **kwargs)

    def _reject_cross_site(self, request: HttpRequest) -> HttpResponse | None:
        """Refuse anything that carries a browser's cross-origin markers.

        The API cannot require a CSRF token, because the client does not send
        one. This is what stands in its place, alongside a SameSite cookie:
        AntennaPod sends neither of these headers, and a browser making a
        cross-origin request always sends at least one. It costs nothing and it
        covers browsers that ignore SameSite.
        """
        if request.headers.get("Origin"):
            return forbidden("cross-origin requests are not accepted")
        if request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            return forbidden("cross-site requests are not accepted")
        return None

    def _authenticate(self, request: HttpRequest) -> AbstractBaseUser | None:
        session_user: Any = getattr(request, "user", None)
        if isinstance(session_user, AbstractBaseUser) and session_user.is_authenticated:
            return session_user
        return authenticate_with_basic(request)

    @property
    def account_id(self) -> int:
        return int(self.account.pk)

    def since(self, request: HttpRequest) -> int | None:
        """Read the `since` cursor, or None if it is not a number.

        Absent means zero: a client syncing for the first time asks for
        everything. Present but unreadable is a client bug worth reporting rather
        than papering over, because silently treating it as zero would resend the
        entire history.
        """
        raw = request.GET.get("since")
        if raw is None or raw == "":
            return 0

        # Plain ASCII digits and nothing else. int() also accepts "1_000", "+5",
        # surrounding whitespace and Arabic-Indic digits, none of which any client
        # sends and all of which would be read as a number the sender did not
        # write. A negative cursor is not a smaller cursor; it is a malformed one.
        if not (raw.isascii() and raw.isdigit()):
            return None

        value = int(raw)
        # The cursor is echoed back and the client reads it with getLong, which
        # is a signed 64-bit value. Returning anything larger would hand a phone a
        # number it cannot parse, and it would never sync again.
        return value if value <= MAX_CURSOR else None

    def json_body(self, request: HttpRequest) -> Any:
        try:
            return json.loads(request.body or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("request body is not valid JSON") from exc


def authenticate_with_basic(request: HttpRequest) -> AbstractBaseUser | None:
    """Check an `Authorization: Basic` header, through Django's backend stack.

    Never `User.objects.get()` plus `check_password()`. Two things depend on
    going the long way round: axes only counts attempts that pass through the
    backend, and `ModelBackend` runs the hasher against a dummy user when the
    username does not exist, which is what stops the response time from saying
    whether it does.
    """
    credentials = decode_basic_auth(request.headers.get("Authorization"))
    if credentials is None:
        return None
    user = authenticate(
        request=request, username=credentials.username, password=credentials.password
    )
    return user if isinstance(user, AbstractBaseUser) else None
