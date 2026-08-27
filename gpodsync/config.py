"""Reading configuration from the environment, and refusing to guess.

Every value here fails closed. That is a deliberate posture rather than caution
for its own sake: this image is run by people on their own servers, and the
failure modes of a wrong guess are all silent. A generated-on-boot secret key
invalidates every session on restart and surfaces as intermittent 401s; a default
`ALLOWED_HOSTS` of `*` turns a misconfigured proxy into an open one. A container
that refuses to start is a bad afternoon; either of those is a bug report nobody
can reproduce.
"""

import os
import secrets
from pathlib import Path
from typing import Final

TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES: Final = frozenset({"0", "false", "no", "off"})

# S105 reads any name containing "key" assigned a literal as a leaked credential.
# This is the name of the file the key is kept in, not the key.
SECRET_KEY_FILENAME: Final = "secret_key"  # noqa: S105
SECRET_KEY_MODE: Final = 0o600


class ConfigurationError(Exception):
    """The container cannot start with the configuration it was given."""


def env_text(name: str, default: str | None = None) -> str | None:
    """Read a variable, or the contents of the file named by `<NAME>_FILE`.

    The `_FILE` form is how docker secrets and compose secrets are delivered:
    the value lands in a file rather than the environment, where it cannot be
    read out of `docker inspect` or a process listing.
    """
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(
                f"{name}_FILE is set but {path} cannot be read: {exc}"
            ) from exc

    value = os.environ.get(name)
    return default if value is None else value


def env_bool(name: str, *, default: bool) -> bool:
    raw = env_text(name)
    # An empty value means unset, not false — matching env_int and env_list. The
    # `VAR=` form is ordinary in a compose file, and reading it as false would
    # quietly disable the lockout or the secure-cookie flag.
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{name} must be one of true/false/yes/no/1/0, not {raw!r}. A typo here "
        f"would otherwise be read as 'off', which is the dangerous direction for "
        f"every switch this application has."
    )


def env_int(name: str, *, default: int) -> int:
    raw = env_text(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a whole number, not {raw!r}") from exc


def env_list(name: str, *, default: list[str] | None = None) -> list[str]:
    raw = env_text(name)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_secret_key(data_dir: Path) -> str:
    """Take the configured key, or mint one once and keep it.

    Never a fresh key per boot. The symptom of that is a user who is signed out
    at random, reported as "it stops syncing sometimes" — one of the hardest
    complaints to act on, and entirely self-inflicted.
    """
    configured = env_text("GPODSYNC_SECRET_KEY")
    if configured and configured.strip():
        return configured.strip()

    key_file = data_dir / SECRET_KEY_FILENAME
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(64)
        # Written with restrictive permissions before the content, so it is never
        # briefly world-readable on a shared volume.
        # O_EXCL so two workers starting together cannot each believe they minted
        # the key, and O_NOFOLLOW so a symlink planted in the data directory
        # cannot redirect a mode-600 write somewhere else.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            with os.fdopen(os.open(key_file, flags, SECRET_KEY_MODE), "w") as handle:
                handle.write(generated)
        except FileExistsError:
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing:
                # Somebody won the race. Their key is the real one.
                return existing
            # The file is there but empty — an interrupted write, or a volume
            # pre-created by hand. It is ours and it is useless, so replace it
            # rather than returning an empty secret key.
            with os.fdopen(
                os.open(key_file, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, SECRET_KEY_MODE), "w"
            ) as handle:
                handle.write(generated)
    except OSError as exc:
        raise ConfigurationError(
            f"GPODSYNC_SECRET_KEY is unset and {key_file} could not be written ({exc}). "
            f"Set the variable, or mount a writable volume at the data directory."
        ) from exc

    return generated
