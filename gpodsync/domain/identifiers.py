"""Client-supplied identifiers that end up in URLs and log lines."""

import re
from typing import Final

# Explicitly ASCII. Python's \w is unicode-aware, so the previous form accepted
# Arabic-Indic digits, CJK, fullwidth Latin and mathematical alphanumerics — none
# dangerous for this sink, but all of them confusable, and this value names rows
# and appears in log lines where two devices that look identical would be a
# genuinely bad afternoon.
DEVICE_ID_PATTERN: Final = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")

# No legitimate device is named this, and it costs nothing to refuse.
RESERVED_DEVICE_IDS: Final = frozenset({".", ".."})


class InvalidDeviceId(ValueError):
    """A device identifier that cannot be accepted from a URL path."""


def validate_device_id(value: str) -> str:
    """Check a device identifier from a URL path.

    Deliberately narrow. This value is chosen by the client, appears in URLs and
    in traces, and is used to name rows; keeping it to word characters, dots and
    hyphens removes path traversal, control characters and log forgery from the
    list of things every consumer of it has to think about.
    """
    if not DEVICE_ID_PATTERN.match(value) or value in RESERVED_DEVICE_IDS:
        raise InvalidDeviceId(f"device identifier {value!r} is not acceptable")
    return value
