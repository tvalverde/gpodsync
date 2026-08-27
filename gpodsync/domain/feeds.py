"""Feed URLs: validated, minimally normalised, never fetched.

These are opaque strings supplied by whoever controls the client. The invariant
in CLAUDE.md — that nothing in this server ever dereferences one — is what keeps
them from being a server-side request forgery vector, and it is an invariant
precisely because "validate the feed by downloading it" is such a reasonable
thing to propose.

Normalisation is deliberately minimal: scheme and host lowercased, surrounding
whitespace removed, nothing else. Every change produces an entry in
`update_urls`, which today's AntennaPod parses and then ignores — but the field
is a standing instruction to rewrite a subscription, and a server that fills it
with noise is relying on the client never implementing it.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH: Final = 2048
ALLOWED_SCHEMES: Final = frozenset({"http", "https"})


class InvalidFeedUrl(ValueError):
    """A subscription URL that will not be stored."""


@dataclass(frozen=True, slots=True)
class SanitisedUrls:
    urls: tuple[str, ...]
    update_pairs: tuple[tuple[str, str], ...]


def sanitise_feed_url(raw: str) -> str:
    """Validate one subscription URL and return its normalised form."""
    text = raw.strip()

    if not text:
        raise InvalidFeedUrl("a subscription URL cannot be empty")
    if len(text) > MAX_URL_LENGTH:
        raise InvalidFeedUrl(f"a subscription URL cannot exceed {MAX_URL_LENGTH} characters")
    if any(character.isspace() or _is_control(character) for character in text):
        raise InvalidFeedUrl("a subscription URL cannot contain whitespace or control characters")

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidFeedUrl(f"scheme {parts.scheme!r} is not allowed")
    if not parts.netloc:
        raise InvalidFeedUrl("a subscription URL needs a host")

    return urlunsplit((scheme, _normalise_netloc(parts.netloc), parts.path, parts.query, ""))


def sanitise_feed_urls(raw_urls: Iterable[str]) -> SanitisedUrls:
    """Normalise a batch, reporting only the URLs that actually changed.

    A pair whose two halves are identical is not merely redundant: the client
    rewrites its subscription to the second half, so telling it to replace a URL
    with itself is churn it cannot distinguish from a real correction.
    """
    urls: list[str] = []
    pairs: list[tuple[str, str]] = []

    for raw in raw_urls:
        sanitised = sanitise_feed_url(raw)
        urls.append(sanitised)
        if sanitised != raw:
            pairs.append((raw, sanitised))

    return SanitisedUrls(urls=tuple(urls), update_pairs=tuple(pairs))


def _normalise_netloc(netloc: str) -> str:
    """Lowercase the host without touching any credentials in front of it.

    Lowercasing the whole authority would also lowercase `user:Password@`, which
    silently breaks authenticated feeds — the password is case-sensitive and the
    host is not.
    """
    userinfo, separator, hostport = netloc.rpartition("@")
    return f"{userinfo}{separator}{hostport.lower()}"


def _is_control(character: str) -> bool:
    codepoint = ord(character)
    return codepoint < 0x20 or codepoint == 0x7F
