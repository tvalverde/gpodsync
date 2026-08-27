"""Redaction: hide the values, keep the shape.

A trace that hid the interesting parts would defeat the reason tracing exists,
so these tests assert as much about what survives as about what is masked.
"""

import time

import pytest

from gpodsync.domain.redaction import (
    REDACTED,
    redact_cookie,
    redact_header,
    redact_headers,
    redact_set_cookie,
    redact_text,
)

pytestmark = pytest.mark.unit


class TestSetCookie:
    """The attributes are the whole diagnostic value of this header."""

    def test_masks_the_value_and_keeps_every_attribute(self):
        redacted = redact_set_cookie(
            "sessionid=s3cr3tvalue; Path=/; Secure; HttpOnly; SameSite=Lax"
        )
        assert redacted == f"sessionid={REDACTED}; Path=/; Secure; HttpOnly; SameSite=Lax"
        assert "s3cr3tvalue" not in redacted

    def test_keeps_the_two_attributes_that_break_the_client(self):
        # Domain and Secure are precisely what has to be inspected when a client
        # logs in successfully and is then refused everything afterwards.
        redacted = redact_set_cookie("sessionid=abc; Domain=.example.com; Secure")
        assert "Domain=.example.com" in redacted
        assert "Secure" in redacted

    def test_a_bare_cookie_with_no_attributes(self):
        assert redact_set_cookie("sessionid=abc") == f"sessionid={REDACTED}"

    def test_a_malformed_value_is_masked_whole(self):
        assert redact_set_cookie("garbage-with-no-equals") == REDACTED


class TestCookie:
    def test_keeps_the_names_and_masks_the_values(self):
        # Whether the client sent a sessionid at all is the question being asked;
        # what was in it never is.
        assert redact_cookie("sessionid=abc; theme=dark") == (
            f"sessionid={REDACTED}; theme={REDACTED}"
        )

    def test_masks_a_chunk_that_is_not_a_pair(self):
        assert redact_cookie("orphan") == REDACTED

    def test_ignores_empty_chunks(self):
        assert redact_cookie("a=1;;") == f"a={REDACTED}"


class TestHeaders:
    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "authorization",
            "Proxy-Authorization",
            "X-Api-Key",
            "X-Session-Token",
            "X-Client-Secret",
            "X-User-Password",
        ],
    )
    def test_masks_anything_credential_shaped(self, name):
        assert redact_header(name, "Basic dG9uaTpodW50ZXIy") == REDACTED

    def test_dispatches_set_cookie_to_the_attribute_preserving_form(self):
        assert redact_header("Set-Cookie", "sessionid=abc; Secure") == (
            f"sessionid={REDACTED}; Secure"
        )

    def test_dispatches_cookie_to_the_name_preserving_form(self):
        assert redact_header("Cookie", "sessionid=abc") == f"sessionid={REDACTED}"

    @pytest.mark.parametrize("name", ["WWW-Authenticate", "Proxy-Authenticate"])
    def test_leaves_challenges_legible(self, name):
        # A challenge carries no secret, and which scheme the server offered is
        # exactly the kind of thing a trace is opened to find out.
        assert redact_header(name, 'Basic realm="gpodsync"') == 'Basic realm="gpodsync"'

    def test_leaves_ordinary_headers_readable(self):
        assert redact_header("Content-Type", "application/json") == "application/json"
        assert redact_header("User-Agent", "AntennaPod/3.5.0") == "AntennaPod/3.5.0"

    def test_redacts_a_whole_mapping(self):
        redacted = redact_headers(
            {"Authorization": "Basic abc", "Content-Type": "plain/text; charset=utf-8"}
        )
        assert redacted["Authorization"] == REDACTED
        assert redacted["Content-Type"] == "plain/text; charset=utf-8"

    def test_an_ordinary_header_still_has_its_contents_swept(self):
        assert (
            redact_header("Referer", "https://user:pw@example.com/f")
            == f"https://{REDACTED}@example.com/f"
        )


class TestFreeText:
    """Credentials arrive inside values no header rule would ever inspect."""

    def test_masks_credentials_inside_a_feed_url(self):
        # Authenticated feeds really are written this way, and the URL is stored
        # and echoed back, so it reaches the log through the body rather than a
        # header.
        body = '{"add": ["https://toni:hunter2@example.com/feed.xml"]}'
        redacted = redact_text(body)
        assert "hunter2" not in redacted
        assert "toni" not in redacted
        assert "example.com/feed.xml" in redacted

    def test_masks_a_username_with_no_password(self):
        assert redact_text("https://toni@example.com/f") == f"https://{REDACTED}@example.com/f"

    def test_leaves_an_ordinary_url_alone(self):
        assert redact_text("https://example.com/feed.xml") == "https://example.com/feed.xml"

    @pytest.mark.parametrize(
        "query",
        ["?token=abc123", "?api_key=abc123", "&password=abc123", "?authToken=abc123"],
    )
    def test_masks_sensitive_query_values(self, query):
        redacted = redact_text(f"https://example.com/f{query}")
        assert "abc123" not in redacted
        assert REDACTED in redacted

    @pytest.mark.parametrize("text", ["token=SECRET&x=1", "access_token=SECRET", "password=SECRET"])
    def test_masks_the_first_parameter_with_no_separator_in_front(self, text):
        # QUERY_STRING arrives without a leading `?` and a form body starts with
        # its first field bare, so requiring a separator meant the first
        # parameter — the one most likely to be the interesting one — was never
        # masked at all.
        assert "SECRET" not in redact_text(text)

    @pytest.mark.parametrize(
        "query", ["?since=42", "?authors=dickens", "?keywords=history", "?monkey=yes"]
    )
    def test_keeps_ordinary_query_values(self, query):
        # authors and keywords contain "auth" and "key" as substrings. Masking
        # them was the first implementation, and they are plausible parameters on
        # a podcast feed.
        assert redact_text(f"https://example.com/f{query}") == f"https://example.com/f{query}"

    def test_a_cookie_value_containing_a_semicolon_does_not_leak_its_tail(self):
        redacted = redact_set_cookie('sessionid="abc;def"; Path=/; Secure')
        assert "def" not in redacted
        assert "Path=/" in redacted

    def test_masks_every_occurrence(self):
        redacted = redact_text("https://a:1@x.com/f https://b:2@y.com/g")
        assert "a:1" not in redacted
        assert "b:2" not in redacted


class TestAdversarialInput:
    """Redaction runs on attacker-supplied bodies, once per request.

    Both patterns were quadratic: the engine restarted at every position of a
    long run, and 80,000 plain letters took 12.7 seconds. With Django's body
    limit that is a worker pinned for minutes by a request full of junk. The
    bound below is generous by two orders of magnitude — it is here to catch a
    reintroduced unbounded quantifier, not to measure performance.
    """

    @pytest.mark.parametrize(
        ("name", "payload"),
        [
            ("a run of letters, no scheme and no at-sign", "a" * 200_000),
            ("a run of query separators", "?" * 200_000),
            ("a run of at-signs", "@" * 200_000),
            ("a plausible scheme prefix repeated", "http://" * 20_000),
        ],
    )
    def test_redaction_stays_linear(self, name, payload):
        started = time.monotonic()
        redact_text(payload)
        assert time.monotonic() - started < 2.0, name


class TestLoggingCannotBreakARequest:
    """The formatter runs inside every request. It must not be able to raise."""

    def test_a_message_with_missing_arguments_degrades(self):
        import json as _json
        import logging

        from gpodsync.logging_config import JsonFormatter

        record = logging.LogRecord("t", logging.INFO, "", 0, "%s and %s", ("only-one",), None)
        rendered = _json.loads(JsonFormatter().format(record))
        assert rendered["logger"] == "gpodsync.logging"

    def test_an_unserialisable_extra_key_degrades(self):
        import json as _json
        import logging

        from gpodsync.logging_config import JsonFormatter

        record = logging.LogRecord("t", logging.INFO, "", 0, "fine", (), None)
        record.__dict__[("a", "tuple")] = "value"
        rendered = _json.loads(JsonFormatter().format(record))
        assert rendered["logger"] == "gpodsync.logging"


class TestTheHealthFilterOnlyJudgesRequestRecords:
    def test_another_logger_carrying_a_health_path_survives(self):
        import logging

        from gpodsync.logging_config import HealthProbeFilter

        record = logging.LogRecord("something.else", logging.INFO, "", 0, "x", (), None)
        record.path = "/healthz/"
        assert HealthProbeFilter().filter(record) is True

    def test_a_status_that_is_not_a_number_is_kept(self):
        import logging

        from gpodsync.logging_config import HealthProbeFilter

        record = logging.LogRecord("gpodsync.request", logging.INFO, "", 0, "x", (), None)
        record.path = "/healthz/"
        record.status = "not-a-status"
        assert HealthProbeFilter().filter(record) is True
