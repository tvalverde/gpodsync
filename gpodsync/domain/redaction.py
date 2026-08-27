"""Masking secrets before anything is written to a log.

Request tracing exists because this server has to be diagnosed against a phone
that disagrees with it, and a trace that omitted the interesting parts would be
useless. So the rule is not "hide anything sensitive-looking" but "hide the
values, keep the shape".

`Set-Cookie` is the case that matters most. Its attributes are exactly what has
to be inspected when a client logs in and is then refused — `Domain` and `Secure`
decide whether AntennaPod will ever send the cookie back — so the attributes stay
readable and only the value is masked. The same applies to the request `Cookie`
header: which cookie names arrived is the diagnostic question, and their contents
never are.
"""

import re
from collections.abc import Mapping
from typing import Final

REDACTED: Final = "[redacted]"

SENSITIVE_HEADERS: Final = frozenset({"authorization", "proxy-authorization"})

# Challenges, not credentials. `WWW-Authenticate: Basic realm="gpodsync"` carries
# nothing secret, and masking it hides which scheme the server offered — which is
# the sort of thing a trace is opened to find out.
CHALLENGE_HEADERS: Final = frozenset({"www-authenticate", "proxy-authenticate"})

# Matched as substrings, case-insensitively, so a header nobody anticipated —
# X-Api-Key, X-Session-Token — is covered by shape rather than by having been
# listed. Being wrong here costs a masked value in a log; being wrong the other
# way costs a credential in one.
SENSITIVE_HEADER_HINTS: Final = ("token", "secret", "password", "api-key", "apikey", "auth")

# Both patterns are bounded on purpose. Unbounded leading quantifiers made these
# quadratic: the engine restarted the scan at every position of a long run, and a
# body of 80,000 plain letters took 12.7 seconds to redact. These run in the
# per-request logging path on attacker-supplied bodies, so that was a remote
# denial of service sitting in a helper module. A scheme is never 32 characters
# and a query-parameter name is never 128, so the caps cost nothing real.
_URL_USERINFO: Final = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://)(?P<userinfo>[^/\s:@]+(?::[^/\s@]*)?)@"
)

# Query parameters are found with a bounded pattern and judged in Python. A regex
# that also decided sensitivity had to express "the word appears as a whole
# token", across both `access_token` and `authToken`, and became unreadable — and
# its unbounded first attempt masked `?authors=` and `?keywords=`, which are
# plausible parameters on a podcast feed. A trace that hides ordinary values is
# worth less than one that shows them.
# The prefix may be the start of the string. QUERY_STRING arrives without a
# leading `?`, and a form body starts with its first field bare — so demanding
# a separator meant the first parameter, the one most likely to be the
# interesting one, was never redacted at all.
_QUERY_PAIR: Final = re.compile(r"(?P<prefix>^|[?&])(?P<key>[^=&?\s]{1,128})=(?P<value>[^&\s]*)")

# Split on delimiters and on camelCase transitions, so `auth_token`, `authToken`
# and `AUTH-TOKEN` all yield the same tokens.
_KEY_TOKENS: Final = re.compile(r"[-_.]|(?<=[a-z0-9])(?=[A-Z])")

SENSITIVE_QUERY_TOKENS: Final = frozenset(
    {
        "token",
        "secret",
        "password",
        "passwd",
        "pass",
        "key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "signature",
        "sig",
        "session",
    }
)


def _is_sensitive_query_key(key: str) -> bool:
    return any(token.lower() in SENSITIVE_QUERY_TOKENS for token in _KEY_TOKENS.split(key))


def _mask_query_pair(match: re.Match[str]) -> str:
    if not _is_sensitive_query_key(match["key"]):
        return match[0]
    return f"{match['prefix']}{match['key']}={REDACTED}"


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Mask the values of headers that carry credentials."""
    return {name: redact_header(name, value) for name, value in headers.items()}


def redact_header(name: str, value: str) -> str:
    lowered = name.lower()
    if lowered == "set-cookie":
        return redact_set_cookie(value)
    if lowered == "cookie":
        return redact_cookie(value)
    if lowered in CHALLENGE_HEADERS:
        return value
    if lowered in SENSITIVE_HEADERS or any(hint in lowered for hint in SENSITIVE_HEADER_HINTS):
        return REDACTED
    return redact_text(value)


def redact_set_cookie(value: str) -> str:
    """Mask the cookie's value while leaving every attribute legible.

    A quoted value may itself contain a semicolon, so the split cannot simply be
    on the first one: doing that leaks the tail of the value into what looks like
    an attribute list.
    """
    name, equals, remainder = value.partition("=")
    if not equals:
        return REDACTED

    body = remainder.lstrip()
    if body.startswith('"'):
        closing = body.find('"', 1)
        attributes = body[closing + 1 :] if closing != -1 else ""
    else:
        separator = remainder.find(";")
        attributes = remainder[separator:] if separator != -1 else ""

    return f"{name.strip()}={REDACTED}{attributes}"


def redact_cookie(value: str) -> str:
    """Mask every cookie value, keeping the names that were sent."""
    masked = []
    for chunk in value.split(";"):
        stripped = chunk.strip()
        if not stripped:
            continue
        name, equals, _ = stripped.partition("=")
        masked.append(f"{name}={REDACTED}" if equals else REDACTED)
    return "; ".join(masked)


def redact_text(text: str) -> str:
    """Mask credentials embedded in free text, such as a body or a query string.

    Subscription URLs are supplied by the client and authenticated feeds really
    are written `https://user:password@host/feed.xml`, so credentials arrive
    inside values that no header-name rule would ever look at.

    Scope, so that nobody assumes more than this does: it sweeps URL userinfo and
    sensitive query values, which is what actually flows through this API. It
    does not parse JSON, so a credential in a body field, or in a URL fragment,
    survives. Nothing in this protocol carries one there.
    """
    without_userinfo = _URL_USERINFO.sub(rf"\g<scheme>{REDACTED}@", text)
    return _QUERY_PAIR.sub(_mask_query_pair, without_userinfo)
