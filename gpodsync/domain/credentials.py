"""Decoding the one credential AntennaPod ever sends.

`Authorization: Basic` reaches exactly one endpoint — login — and nothing after
it, so this is the entire credential surface of the API.

Every malformed header returns None rather than raising, and the distinctions
between them are deliberately thrown away. A caller that could tell "no header"
from "bad base64" from "no colon" would eventually report the difference, and the
login endpoint's whole job is to answer identically however it failed.
"""

from base64 import b64decode
from dataclasses import dataclass
from typing import Final

SCHEME: Final = "basic"


@dataclass(frozen=True, slots=True)
class Credentials:
    username: str
    password: str


def decode_basic_auth(header: str | None) -> Credentials | None:
    """Read an `Authorization: Basic` header, or None if it is unusable."""
    if not header:
        return None

    scheme, separator, encoded = header.partition(" ")
    if not separator or scheme.lower() != SCHEME:
        return None

    try:
        # binascii.Error, which b64decode raises, is itself a ValueError.
        decoded = b64decode(encoded.strip(), validate=True)
    except ValueError:
        return None

    try:
        text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None

    # Only the first colon separates the pair: a password may contain colons, a
    # username may not.
    username, separator, password = text.partition(":")
    if not separator or not username:
        return None

    return Credentials(username=username, password=password)
