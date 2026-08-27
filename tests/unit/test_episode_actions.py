"""The codec, exercised against the client's actual strictness."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from gpodsync.domain.episode_actions import (
    IMPORT_SENTINEL,
    Action,
    EpisodeAction,
    InvalidAction,
    content_key,
    format_timestamp,
    parse_action,
    parse_timestamp,
    serialise_action,
)

pytestmark = pytest.mark.unit


def an_action(**overrides) -> EpisodeAction:
    defaults = {
        "podcast": "https://example.com/feed.xml",
        "episode": "https://example.com/ep1.mp3",
        "action": Action.PLAY,
        "timestamp": datetime(2026, 8, 26, 19, 10, 0, tzinfo=UTC),
    }
    return EpisodeAction(**(defaults | overrides))


class TestTimestampFormat:
    """The format is a fixed SimpleDateFormat on the client. Nothing else parses."""

    def test_renders_without_zone_suffix_or_fraction(self):
        moment = datetime(2026, 8, 26, 19, 10, 5, 123456, tzinfo=UTC)
        assert format_timestamp(moment) == "2026-08-26T19:10:05"

    def test_converts_other_zones_to_utc(self):
        madrid = datetime(2026, 8, 26, 21, 10, 0, tzinfo=timezone(timedelta(hours=2)))
        assert format_timestamp(madrid) == "2026-08-26T19:10:00"

    def test_treats_a_naive_value_as_utc(self):
        assert format_timestamp(datetime(2026, 8, 26, 19, 10, 0)) == "2026-08-26T19:10:00"  # noqa: DTZ001

    def test_pads_a_short_year(self):
        # strftime's zero-padding below year 1000 is platform-dependent, and the
        # published image runs on musl rather than the glibc this was written on.
        assert format_timestamp(datetime(999, 1, 2, 3, 4, 5, tzinfo=UTC)) == "0999-01-02T03:04:05"

    def test_round_trips(self):
        rendered = "2026-08-26T19:10:00"
        assert format_timestamp(parse_timestamp(rendered)) == rendered


class TestTimestampParsing:
    """Liberal on the way in, because accepting more costs nothing here."""

    @pytest.mark.parametrize(
        "value",
        [
            "2026-08-26T19:10:00",
            "2026-08-26T19:10:00Z",
            "2026-08-26T19:10:00z",
            "2026-08-26T19:10:00+00:00",
            "  2026-08-26T19:10:00  ",
        ],
    )
    def test_accepts_the_spellings_a_client_might_send(self, value):
        assert parse_timestamp(value) == datetime(2026, 8, 26, 19, 10, 0, tzinfo=UTC)

    def test_normalises_an_offset_to_utc(self):
        assert parse_timestamp("2026-08-26T21:10:00+02:00") == datetime(
            2026, 8, 26, 19, 10, 0, tzinfo=UTC
        )

    def test_keeps_fractional_seconds_out_of_the_rendered_form(self):
        assert format_timestamp(parse_timestamp("2026-08-26T19:10:00.500")) == "2026-08-26T19:10:00"

    def test_accepts_a_lowercase_separator(self):
        # ISO-8601 allows it. The client never sends it, but rejecting it would
        # contradict this parser's stated liberalism for no gain.
        assert parse_timestamp("2026-08-26t19:10:00") == datetime(
            2026, 8, 26, 19, 10, 0, tzinfo=UTC
        )

    def test_rejects_a_date_with_no_time(self):
        with pytest.raises(InvalidAction, match="no time component"):
            parse_timestamp("2026-08-26")

    def test_rejects_nonsense(self):
        with pytest.raises(InvalidAction, match="not a valid datetime"):
            parse_timestamp("2026-13-99T99:99:99")


class TestPlaybackPosition:
    """The client's guard is `started >= 0 && position > 0 && total > 0`."""

    def test_a_complete_triple_counts(self):
        assert an_action(started=0, position=120, total=500).has_playback_position

    @pytest.mark.parametrize(
        ("started", "position", "total"),
        [
            (-1, 120, 500),
            (0, 0, 500),
            (0, 120, 0),
            (0, -5, 500),
            (0, 120, -1),
        ],
    )
    def test_a_value_outside_the_guard_does_not(self, started, position, total):
        assert not an_action(started=started, position=position, total=total).has_playback_position

    @pytest.mark.parametrize(
        ("started", "position", "total"),
        [(None, 120, 500), (0, None, 500), (0, 120, None)],
    )
    def test_an_incomplete_triple_does_not(self, started, position, total):
        assert not an_action(started=started, position=position, total=total).has_playback_position

    def test_only_play_actions_carry_a_position(self):
        action = an_action(action=Action.DOWNLOAD, started=0, position=120, total=500)
        assert not action.has_playback_position


class TestParsing:
    def test_reads_a_full_upload(self):
        action = parse_action(
            {
                "podcast": "https://example.com/feed.xml",
                "episode": "https://example.com/ep1.mp3",
                "guid": "abc",
                "action": "PLAY",
                "timestamp": "2026-08-26T19:10:00",
                "started": 0,
                "position": 120,
                "total": 500,
                "device": "phone",
            }
        )
        assert action.action is Action.PLAY
        assert action.guid == "abc"
        assert action.device == "phone"
        assert action.has_playback_position

    def test_a_missing_timestamp_is_left_for_the_server_to_stamp(self):
        action = parse_action(
            {"podcast": "https://e.com/f", "episode": "https://e.com/e", "action": "new"}
        )
        assert action.timestamp is None

    @pytest.mark.parametrize("field", ["podcast", "episode"])
    def test_rejects_a_missing_url(self, field):
        raw = {"podcast": "https://e.com/f", "episode": "https://e.com/e", "action": "new"}
        del raw[field]
        with pytest.raises(InvalidAction, match=f"{field} is required"):
            parse_action(raw)

    @pytest.mark.parametrize("value", ["", "   ", 42, None])
    def test_rejects_a_url_that_is_not_usable_text(self, value):
        with pytest.raises(InvalidAction, match="podcast is required"):
            parse_action({"podcast": value, "episode": "https://e.com/e", "action": "new"})

    def test_rejects_an_overlong_url(self):
        with pytest.raises(InvalidAction, match="longer than"):
            parse_action(
                {
                    "podcast": "https://e.com/" + "a" * 4200,
                    "episode": "https://e.com/e",
                    "action": "new",
                }
            )

    def test_rejects_a_missing_action(self):
        with pytest.raises(InvalidAction, match="action is required"):
            parse_action({"podcast": "https://e.com/f", "episode": "https://e.com/e"})

    def test_rejects_an_unknown_action(self):
        with pytest.raises(InvalidAction, match="unknown action"):
            parse_action(
                {"podcast": "https://e.com/f", "episode": "https://e.com/e", "action": "flag"}
            )

    def test_blank_optional_text_becomes_absent(self):
        action = parse_action(
            {
                "podcast": "https://e.com/f",
                "episode": "https://e.com/e",
                "action": "new",
                "guid": "   ",
            }
        )
        assert action.guid is None

    def test_rejects_optional_text_that_is_not_text(self):
        with pytest.raises(InvalidAction, match="guid must be a string"):
            parse_action(
                {
                    "podcast": "https://e.com/f",
                    "episode": "https://e.com/e",
                    "action": "new",
                    "guid": 7,
                }
            )

    @pytest.mark.parametrize("value", [2**31, -(2**31), 10**20, "99999999999"])
    def test_rejects_a_playback_offset_out_of_range(self, value):
        # Past SQLite's integer range this reached the client as a 500 rather
        # than as the rejection it is. The bound is sixty-eight years of audio.
        with pytest.raises(InvalidAction, match="out of range"):
            parse_action(
                {
                    "podcast": "https://e.com/f",
                    "episode": "https://e.com/e",
                    "action": "play",
                    "position": value,
                }
            )

    def test_accepts_an_offset_at_the_edge_of_the_range(self):
        action = parse_action(
            {
                "podcast": "https://e.com/f",
                "episode": "https://e.com/e",
                "action": "play",
                "total": 2**31 - 1,
            }
        )
        assert action.total == 2**31 - 1

    def test_rejects_an_overlong_guid(self):
        # The column says 255 and SQLite does not enforce it, so without this the
        # declared schema is a promise nothing keeps.
        with pytest.raises(InvalidAction, match="longer than 255"):
            parse_action(
                {
                    "podcast": "https://e.com/f",
                    "episode": "https://e.com/e",
                    "action": "new",
                    "guid": "x" * 256,
                }
            )

    def test_accepts_a_guid_at_the_limit(self):
        action = parse_action(
            {
                "podcast": "https://e.com/f",
                "episode": "https://e.com/e",
                "action": "new",
                "guid": "x" * 255,
            }
        )
        assert action.guid == "x" * 255

    def test_accepts_a_numeric_string_position(self):
        action = parse_action(
            {
                "podcast": "https://e.com/f",
                "episode": "https://e.com/e",
                "action": "play",
                "started": "0",
                "position": " 120 ",
                "total": "500",
            }
        )
        assert action.position == 120

    @pytest.mark.parametrize("value", [True, False, 1.5, "abc", []])
    def test_rejects_a_position_that_is_not_a_whole_number(self, value):
        # `True` is the one that matters: bool subclasses int, so without an
        # explicit check it would arrive as position 1 and look like real data.
        with pytest.raises(InvalidAction, match="position must be a whole number"):
            parse_action(
                {
                    "podcast": "https://e.com/f",
                    "episode": "https://e.com/e",
                    "action": "play",
                    "position": value,
                }
            )


class TestSerialising:
    def test_emits_the_mandatory_fields(self):
        assert serialise_action(an_action(action=Action.NEW)) == {
            "podcast": "https://example.com/feed.xml",
            "episode": "https://example.com/ep1.mp3",
            "action": "new",
            "timestamp": "2026-08-26T19:10:00",
        }

    def test_includes_a_position_the_client_will_read(self):
        payload = serialise_action(an_action(started=0, position=120, total=500))
        assert payload["started"] == 0
        assert payload["position"] == 120
        assert payload["total"] == 500

    def test_omits_a_position_the_client_would_discard(self):
        # position 0 is read as a bare `play`, so sending it is worse than
        # sending nothing: it looks like data and vanishes.
        payload = serialise_action(an_action(started=0, position=0, total=500))
        assert "position" not in payload
        assert "started" not in payload
        assert "total" not in payload

    def test_includes_optional_identifiers_when_present(self):
        payload = serialise_action(an_action(guid="abc", device="phone"))
        assert payload["guid"] == "abc"
        assert payload["device"] == "phone"

    def test_refuses_to_send_an_action_with_no_timestamp(self):
        with pytest.raises(InvalidAction, match="must carry a timestamp"):
            serialise_action(an_action(timestamp=None))


class TestImportSentinel:
    """The weakest claim expressible on the wire: epoch zero loses every
    strictly-after comparison the client makes."""

    def test_formats_to_the_exact_wire_shape(self):
        assert format_timestamp(IMPORT_SENTINEL) == "1970-01-01T00:00:00"

    def test_is_aware_utc(self):
        # A naive sentinel would compare wrongly against the aware rows Django
        # stores under USE_TZ.
        assert IMPORT_SENTINEL.tzinfo is UTC

    def test_round_trips_through_the_codec(self):
        assert parse_timestamp(format_timestamp(IMPORT_SENTINEL)) == IMPORT_SENTINEL


class TestContentKey:
    """The identity a re-reported action collapses under. It must agree with the
    database constraint, Coalesce included."""

    def a_key(self, **overrides):
        defaults = {
            "podcast": "https://example.com/feed.xml",
            "episode": "https://example.com/ep1.mp3",
            "action": "play",
            "guid": "abc",
            "started": 0,
            "position": 120,
            "total": 500,
        }
        return content_key(**(defaults | overrides))

    def test_identical_content_yields_identical_keys(self):
        assert self.a_key() == self.a_key()

    @pytest.mark.parametrize("field", ["started", "position", "total"])
    def test_an_absent_offset_collapses_to_minus_one(self, field):
        assert self.a_key(**{field: None}) == self.a_key(**{field: -1})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("podcast", "https://example.com/other.xml"),
            ("episode", "https://example.com/ep2.mp3"),
            ("action", "download"),
            ("guid", "xyz"),
            ("started", 1),
            ("position", 121),
            ("total", 501),
        ],
    )
    def test_each_field_distinguishes(self, field, value):
        assert self.a_key(**{field: value}) != self.a_key()

    def test_a_real_zero_is_not_a_missing_value(self):
        # Paused-at-the-start is a report; no-position-at-all is its absence.
        assert self.a_key(position=0) != self.a_key(position=None)
