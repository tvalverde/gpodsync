"""The episode-action codec.

The wire format is settled by AntennaPod's parser rather than by the gpodder.net
specification, and it is stricter than it looks: a fixed `SimpleDateFormat` that
accepts nothing but `yyyy-MM-dd'T'HH:mm:ss`, lowercase action names, and a
playback triple the client discards unless every part of it is present and
positive. `docs/protocol.md` records the evidence.

Parsing is liberal where being liberal costs nothing — a trailing `Z`, an offset,
fractional seconds, a mixed-case action name are all accepted on the way in.
Serialising is strict, because that output is what a phone has to parse.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%S"

# More generous than the 2048 used for subscription URLs. Enclosure URLs carry
# tracking prefixes stacked by hosting platforms and genuinely exceed it, and
# the client uploads them regardless — rejecting one would discard a real
# listening position rather than protect anything.
MAX_URL_LENGTH: Final = 4096

MAX_GUID_LENGTH: Final = 255

# Playback offsets are seconds. This bound is about sixty-eight years, so nothing
# real approaches it, and past it SQLite refuses the value outright — which
# reached the client as a 500 rather than as the rejection it is.
MAX_POSITION_SECONDS: Final = 2**31 - 1

# The timestamp assigned to actions arriving inside an initial import. A device
# syncing from cursor 0 dumps its whole database stamped with the moment of the
# dump, and the client applies any downloaded action whose timestamp is strictly
# after a competing one — so an import must make the weakest possible claim:
# epoch zero loses every comparison against genuinely-timestamped history.
IMPORT_SENTINEL: Final = datetime(1970, 1, 1, tzinfo=UTC)


class InvalidAction(ValueError):
    """An action the client sent that cannot be stored as it stands."""


class Action(StrEnum):
    NEW = "new"
    DOWNLOAD = "download"
    PLAY = "play"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class EpisodeAction:
    podcast: str
    episode: str
    action: Action
    timestamp: datetime | None = None
    guid: str | None = None
    started: int | None = None
    position: int | None = None
    total: int | None = None
    device: str | None = None

    @property
    def has_playback_position(self) -> bool:
        """Whether AntennaPod will read this action's position at all.

        The client's own guard is `started >= 0 && position > 0 && total > 0`.
        Anything else and it treats the action as a bare `play`, which is why an
        episode paused at the very beginning appears never to sync: position 0 is
        indistinguishable from no position at all.
        """
        if self.action is not Action.PLAY:
            return False
        if self.started is None or self.position is None or self.total is None:
            return False
        return self.started >= 0 and self.position > 0 and self.total > 0


def parse_timestamp(value: str) -> datetime:
    """Read a wire timestamp as an aware UTC datetime."""
    text = value.strip()
    if "T" not in text and "t" not in text:
        raise InvalidAction(f"timestamp {value!r} has no time component")
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidAction(f"timestamp {value!r} is not a valid datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_timestamp(moment: datetime) -> str:
    """Render a timestamp in the only shape AntennaPod's parser accepts.

    No `Z`, no offset, no fractional seconds. A naive value is taken as UTC
    already, since nothing in this application produces local time.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    utc = moment.astimezone(UTC)
    # Built by hand rather than with strftime, whose zero-padding of %Y below
    # year 1000 is platform-dependent — and the published image runs on musl,
    # not the glibc this was written on.
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
    )


def parse_action(raw: Mapping[str, Any]) -> EpisodeAction:
    """Build an action from one uploaded JSON object.

    Raises `InvalidAction` rather than returning a degraded object: an action
    missing its podcast, episode or verb has nowhere to be filed, and guessing
    would put a wrong entry in someone's history.
    """
    timestamp_value = raw.get("timestamp")
    return EpisodeAction(
        podcast=_required_url(raw, "podcast"),
        episode=_required_url(raw, "episode"),
        action=_required_action(raw),
        timestamp=parse_timestamp(timestamp_value) if timestamp_value is not None else None,
        guid=_optional_text(raw, "guid", limit=MAX_GUID_LENGTH),
        started=_optional_int(raw, "started"),
        position=_optional_int(raw, "position"),
        total=_optional_int(raw, "total"),
        device=_optional_text(raw, "device"),
    )


def serialise_action(action: EpisodeAction) -> dict[str, Any]:
    """Render an action for the client.

    The playback triple is emitted only when the client would actually read it.
    Sending `position: 0` is worse than sending nothing: it looks like data and
    is silently discarded.
    """
    if action.timestamp is None:
        raise InvalidAction("an action being sent to a client must carry a timestamp")

    payload: dict[str, Any] = {
        "podcast": action.podcast,
        "episode": action.episode,
        "action": action.action.value,
        "timestamp": format_timestamp(action.timestamp),
    }
    if action.guid is not None:
        payload["guid"] = action.guid
    if action.device is not None:
        payload["device"] = action.device
    if action.has_playback_position:
        payload["started"] = action.started
        payload["position"] = action.position
        payload["total"] = action.total
    return payload


def content_key(
    *,
    podcast: str,
    episode: str,
    action: str,
    guid: str,
    started: int | None,
    position: int | None,
    total: int | None,
) -> tuple[str, str, str, str, int, int, int]:
    """The identity under which a re-reported action collapses.

    Absent offsets map to -1, mirroring the Coalesce in the database constraint,
    so the migration's dedupe and the constraint agree on what "the same
    content" means. A real 0 stays 0: paused-at-the-start is not the same
    report as no-position-at-all.
    """
    return (
        podcast,
        episode,
        action,
        guid,
        -1 if started is None else started,
        -1 if position is None else position,
        -1 if total is None else total,
    )


def _required_url(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise InvalidAction(f"{field} is required")
    text = value.strip()
    if len(text) > MAX_URL_LENGTH:
        raise InvalidAction(f"{field} is longer than {MAX_URL_LENGTH} characters")
    return text


def _required_action(raw: Mapping[str, Any]) -> Action:
    value = raw.get("action")
    if not isinstance(value, str):
        raise InvalidAction("action is required")
    try:
        return Action(value.strip().lower())
    except ValueError as exc:
        raise InvalidAction(f"unknown action {value!r}") from exc


def _optional_text(raw: Mapping[str, Any], field: str, *, limit: int | None = None) -> str | None:
    value = raw.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidAction(f"{field} must be a string")
    text = value.strip()
    if limit is not None and len(text) > limit:
        # The column says 255 and SQLite does not enforce it, so without this the
        # declared schema is a promise nothing keeps — and a page of a thousand
        # rows could be assembled from megabyte-long values.
        raise InvalidAction(f"{field} is longer than {limit} characters")
    return text or None


def _optional_int(raw: Mapping[str, Any], field: str) -> int | None:
    value = raw.get(field)
    if value is None:
        return None
    # bool is a subclass of int, and `True` would otherwise arrive as position 1.
    if isinstance(value, bool):
        raise InvalidAction(f"{field} must be a whole number")
    if isinstance(value, int):
        return _within_range(value, field)
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise InvalidAction(f"{field} must be a whole number") from exc
        # Outside the try on purpose: InvalidAction subclasses ValueError, so a
        # range failure raised inside it was caught here and relabelled as a
        # parse failure.
        return _within_range(parsed, field)
    raise InvalidAction(f"{field} must be a whole number")


def _within_range(value: int, field: str) -> int:
    if not -MAX_POSITION_SECONDS <= value <= MAX_POSITION_SECONDS:
        raise InvalidAction(f"{field} is out of range for a playback offset")
    return value
