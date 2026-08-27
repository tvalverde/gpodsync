"""Request tracing, for the conversation this server is most likely to lose.

This exists because the hard failures in this protocol are silent ones: a cookie
the phone discarded without a word, a field it parsed differently than expected.
None of them produce a stack trace. The only way to see them is to look at what
was actually sent and what actually came back.

Which means the trace contains everything — headers, both bodies, the lot — and
so it is off by default, redacted on the way out, and deliberately independent of
debug mode. Someone diagnosing a sync failure on a live server needs the trace;
they do not need tracebacks rendered to the internet at the same time.
"""

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, HttpResponse, UnreadablePostError

from gpodsync.domain.redaction import redact_headers, redact_set_cookie, redact_text
from gpodsync.logging_config import request_id

logger = logging.getLogger("gpodsync.request")

# Enough to hold a full batch of thirty episode actions, which is about ten
# kilobytes, and short enough that a body of junk cannot fill a disk one log line
# at a time.
DEFAULT_BODY_LIMIT = 8192

TRUNCATION_NOTE = "…[truncated]"
UNREADABLE = "[unreadable]"
TOO_LARGE = "[rejected: larger than DATA_UPLOAD_MAX_MEMORY_SIZE]"


class RequestTracing:
    """Logs one line per request, and a full transaction when tracing is on."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.tracing = bool(getattr(settings, "TRACE_REQUESTS", False))
        self.body_limit = int(getattr(settings, "TRACE_BODY_LIMIT", DEFAULT_BODY_LIMIT))

        if self.tracing:
            # Said out loud at startup, because the cost of forgetting is months
            # of somebody's listening history sitting in a log file.
            logger.warning(
                "Request tracing is on. Every request and response body is being "
                "logged, including every subscription URL and listening position. "
                "Turn it off once you have what you need.",
                extra={"event": "tracing_enabled"},
            )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        token = request_id.set(uuid.uuid4().hex[:16])
        started = time.monotonic()

        # Read before the view consumes it. Django caches the body, so this does
        # not deprive the handler of it.
        request_body = self._request_body(request) if self.tracing else None

        response = self.get_response(request)

        duration_ms = round((time.monotonic() - started) * 1000, 1)
        current = request_id.get()
        if current is not None:
            # Lets a line here be matched against the reverse proxy's own log.
            response.headers["X-Request-Id"] = current

        try:
            self._log(request, response, request_body, duration_ms)
        finally:
            request_id.reset(token)

        return response

    def _log(
        self,
        request: HttpRequest,
        response: HttpResponse,
        request_body: str | None,
        duration_ms: float,
    ) -> None:
        details: dict[str, Any] = {
            "event": "request",
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        }

        if not self.tracing:
            logger.info(
                "%s %s %s", request.method, request.path, response.status_code, extra=details
            )
            return

        details["query"] = redact_text(request.META.get("QUERY_STRING", ""))
        details["request_headers"] = redact_headers(self._headers(request))
        details["request_body"] = request_body
        details["response_headers"] = redact_headers(dict(response.headers))
        details["response_body"] = self._response_body(response)

        # Set-Cookie is not in response.headers when several are set, and its
        # attributes are the single most useful thing in this whole trace: Domain
        # and Secure decide whether the client will ever send the cookie back.
        cookies = [
            redact_set_cookie(morsel.OutputString())
            for morsel in getattr(response, "cookies", {}).values()
        ]
        if cookies:
            details["set_cookie"] = cookies

        logger.info("%s %s %s", request.method, request.path, response.status_code, extra=details)

    def _headers(self, request: HttpRequest) -> dict[str, str]:
        return dict(request.headers.items())

    def _request_body(self, request: HttpRequest) -> str:
        try:
            raw = request.body
        except RequestDataTooBig:
            # The trace should say the body was refused rather than take the
            # request down with it.
            return TOO_LARGE
        except UnreadablePostError, OSError:
            return UNREADABLE
        return self._decode(raw)

    def _response_body(self, response: HttpResponse) -> str:
        if getattr(response, "streaming", False):
            # Consuming it here would leave nothing for the client.
            return "[streaming]"
        return self._decode(response.content)

    def _decode(self, raw: bytes) -> str:
        truncated = raw[: self.body_limit]
        # errors="replace" rather than a failure: a body that is not UTF-8 is
        # exactly the kind of thing worth seeing in a trace.
        text = truncated.decode("utf-8", errors="replace")
        if len(raw) > self.body_limit:
            text += TRUNCATION_NOTE
        return redact_text(text)
