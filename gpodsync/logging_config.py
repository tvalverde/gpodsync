"""Structured logging that attacker-supplied text cannot forge.

Every line is one JSON object built with `json.dumps`, never by interpolation.
That is not a formatting preference. Request bodies, feed URLs and device names
all reach the log, all come from whoever is talking to the server, and a newline
in any of them would otherwise end the line early and let the next characters
appear as a log entry of their own making. `ensure_ascii=True` escapes every
control character and every non-ASCII byte, so a record occupies exactly one
line whatever it contains.
"""

import json
import logging
from contextvars import ContextVar
from typing import Any, Final

# Carried across the whole handling of one request, including the domain logs a
# view emits, so a trace can be read as a conversation rather than as interleaved
# fragments from concurrent syncs.
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

RESERVED: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Logging must never be the thing that fails a request. A mismatched
        # `%s` and a non-string key in `extra` both raise before or inside
        # json.dumps — `default=` rescues unserialisable values, not keys — and
        # either would surface as a 500 for a request that had already worked.
        try:
            return self._render(record)
        except Exception as failure:
            return json.dumps(
                {
                    "level": "ERROR",
                    "logger": "gpodsync.logging",
                    "message": "a log record could not be rendered",
                    "reason": str(failure),
                    "record": record.name,
                },
                ensure_ascii=True,
            )

    def _render(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        current = request_id.get()
        if current is not None:
            payload["request_id"] = current

        # Anything a caller attached with `extra=`, which is how the tracing
        # middleware carries a whole transaction.
        payload.update(
            {key: value for key, value in record.__dict__.items() if key not in RESERVED}
        )

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str so an unserialisable value degrades to its repr instead of
        # taking down the logging call — and with it, the request.
        return json.dumps(payload, ensure_ascii=True, default=str)


class HealthProbeFilter(logging.Filter):
    """Drops successful health checks, and only those.

    Docker probes this every thirty seconds forever. Keeping those lines buries
    the ones that matter; dropping them unconditionally would hide the moment the
    probe started failing, which is the only time anybody wants to see it.
    """

    #: Only the request logger's own records are candidates. Without this, any
    #: record from any logger that happened to carry a `path` of "/healthz/" —
    #: an `extra` somebody adds years from now — would vanish from the log.
    REQUEST_LOGGER = "gpodsync.request"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != self.REQUEST_LOGGER:
            return True
        if getattr(record, "path", None) != "/healthz/":
            return True
        try:
            status = int(getattr(record, "status", 0))
        except TypeError, ValueError:
            # Not a status we can judge, so not one to drop.
            return True
        return status >= 400
