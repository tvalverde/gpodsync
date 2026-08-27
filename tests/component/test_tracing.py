"""What the trace must contain, and what it must never contain.

Both halves matter equally. A trace that hid the cookie attributes would be
useless for the failure this project exists to fix; one that leaked a password
would be a liability sitting in a log file for months.
"""

import io
import json
import logging
from base64 import b64encode

import pytest
from django.contrib.auth import get_user_model

from gpodsync.logging_config import HealthProbeFilter, JsonFormatter

pytestmark = [pytest.mark.component, pytest.mark.django_db]

USERNAME = "toni"
PASSWORD = "a-sufficiently-long-passphrase"
LOGIN = f"/api/2/auth/{USERNAME}/login.json"


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username=USERNAME, password=PASSWORD)


class Captured:
    """The console handler, redirected into memory and parsed back."""

    def __init__(self, stream: io.StringIO) -> None:
        self.stream = stream

    @property
    def raw(self) -> str:
        return self.stream.getvalue()

    @property
    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.raw.splitlines() if line.strip()]

    def requests(self) -> list[dict]:
        return [line for line in self.lines if line.get("event") == "request"]


@pytest.fixture
def captured():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(HealthProbeFilter())

    root = logging.getLogger()
    previous = root.handlers[:]
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    try:
        yield Captured(stream)
    finally:
        root.handlers = previous


@pytest.fixture
def tracing(settings):
    settings.TRACE_REQUESTS = True
    return settings


def basic(username=USERNAME, password=PASSWORD) -> str:
    return "Basic " + b64encode(f"{username}:{password}".encode()).decode()


def login(client, **headers):
    return client.post(
        LOGIN,
        data="",
        content_type="plain/text; charset=utf-8",
        headers={"authorization": basic(), **headers},
    )


class TestNothingSecretEscapes:
    def test_the_authorization_header_never_appears(self, account, client, captured, tracing):
        login(client)
        assert PASSWORD not in captured.raw
        assert basic() not in captured.raw
        assert b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode() not in captured.raw

    def test_the_session_cookie_value_never_appears(self, account, client, captured, tracing):
        response = login(client)
        issued = response.cookies["sessionid"].value
        assert issued not in captured.raw

    def test_credentials_inside_a_feed_url_never_appear(self, account, client, captured, tracing):
        # Authenticated feeds are written this way, and the URL travels in the
        # body — past every rule that looks at header names.
        client.force_login(account)
        client.post(
            f"/api/2/subscriptions/{USERNAME}/phone.json",
            data=json.dumps({"add": ["https://someone:hunter2@example.com/f.xml"], "remove": []}),
            content_type="application/json",
        )
        assert "hunter2" not in captured.raw

    def test_the_cookie_header_sent_back_is_masked(self, account, client, captured, tracing):
        client.force_login(account)
        client.get(f"/api/2/devices/{USERNAME}.json")
        for line in captured.requests():
            cookie = line.get("request_headers", {}).get("Cookie", "")
            assert "sessionid=" not in cookie or "[redacted]" in cookie


class TestWhatTheTraceMustKeep:
    def test_the_cookie_attributes_stay_legible(self, account, client, captured, tracing):
        # Domain and Secure decide whether AntennaPod will ever send the cookie
        # back. Hiding them would defeat the reason this middleware exists.
        login(client)
        traces = [line for line in captured.requests() if "set_cookie" in line]
        assert traces
        emitted = " ".join(traces[-1]["set_cookie"])
        assert "Path=/" in emitted
        assert "sessionid=[redacted]" in emitted

    def test_both_bodies_are_recorded(self, account, client, captured, tracing):
        client.force_login(account)
        client.post(
            f"/api/2/subscriptions/{USERNAME}/phone.json",
            data=json.dumps({"add": ["https://example.com/a.xml"], "remove": []}),
            content_type="application/json",
        )
        trace = captured.requests()[-1]
        assert "example.com/a.xml" in trace["request_body"]
        assert "update_urls" in trace["response_body"]

    def test_the_status_and_duration_are_recorded(self, account, client, captured, tracing):
        login(client)
        trace = captured.requests()[-1]
        assert trace["status"] == 200
        assert isinstance(trace["duration_ms"], int | float)


class TestOneLinePerRecord:
    """Attacker-supplied text must not be able to forge a log entry."""

    def test_a_body_full_of_newlines_stays_on_one_line(self, account, client, captured, tracing):
        client.force_login(account)
        forged = '{"add": ["https://e.com/a\\n{\\"level\\": \\"CRITICAL\\"}"], "remove": []}'
        client.post(
            f"/api/2/subscriptions/{USERNAME}/phone.json",
            data=forged,
            content_type="application/json",
        )
        # Every line still parses as one JSON object; nothing was split, so the
        # forged fragment never appears where a reader would take it for a record.
        assert captured.lines
        for line in captured.raw.splitlines():
            if line.strip():
                json.loads(line)

    def test_no_raw_control_character_is_ever_emitted(self, account, client, captured, tracing):
        client.force_login(account)
        client.post(
            f"/api/2/subscriptions/{USERNAME}/phone.json",
            data=b'{"add": ["https://e.com/\x1b[2J\x00\r\n"], "remove": []}',
            content_type="application/json",
        )
        body = captured.raw
        for forbidden in ("\x1b", "\x00", "\r"):
            assert forbidden not in body

    def test_the_output_is_ascii_only(self, account, client, captured, tracing):
        # Sent as raw bytes on purpose. Building this with json.dumps would
        # escape the non-ASCII before it ever left the test, and the assertion
        # would hold no matter what the formatter did — which is exactly how the
        # first version of this test passed against ensure_ascii=False.
        client.force_login(account)
        client.post(
            f"/api/2/subscriptions/{USERNAME}/phone.json",
            data='{"add": ["https://ex\u00e1mple.com/\u00f1.xml"], "remove": []}'.encode(),
            content_type="application/json",
        )
        # A raw string on purpose: what ensure_ascii produces is the six
        # characters of the escape sequence, not the character itself.
        assert r"\u00e1" in captured.raw
        assert captured.raw.isascii()


class TestOffByDefault:
    def test_without_tracing_no_body_is_logged(self, account, client, captured, settings):
        settings.TRACE_REQUESTS = False
        client.force_login(account)
        client.post(
            f"/api/2/subscriptions/{USERNAME}/phone.json",
            data=json.dumps({"add": ["https://example.com/secret-feed.xml"], "remove": []}),
            content_type="application/json",
        )
        assert "secret-feed" not in captured.raw

    def test_a_summary_line_is_still_produced(self, account, client, captured, settings):
        settings.TRACE_REQUESTS = False
        client.force_login(account)
        client.get(f"/api/2/devices/{USERNAME}.json")
        trace = captured.requests()[-1]
        assert trace["status"] == 200
        assert "request_headers" not in trace


class TestCorrelation:
    def test_every_request_carries_an_identifier(self, account, client, captured):
        client.force_login(account)
        response = client.get(f"/api/2/devices/{USERNAME}.json")
        assert response.headers["X-Request-Id"]
        assert captured.requests()[-1]["request_id"] == response.headers["X-Request-Id"]

    def test_two_requests_do_not_share_one(self, account, client, captured):
        client.force_login(account)
        first = client.get(f"/api/2/devices/{USERNAME}.json")
        second = client.get(f"/api/2/devices/{USERNAME}.json")
        assert first.headers["X-Request-Id"] != second.headers["X-Request-Id"]


class TestHealthProbes:
    def test_a_passing_probe_is_not_logged(self, client, captured):
        client.get("/healthz/")
        assert not [line for line in captured.requests() if line["path"] == "/healthz/"]

    def test_a_failing_probe_is_logged(self, captured):
        # Dropping these unconditionally would hide the only moment anybody wants
        # to see them.
        record = logging.LogRecord("gpodsync.request", logging.INFO, "", 0, "x", (), None)
        record.path = "/healthz/"
        record.status = 503
        assert HealthProbeFilter().filter(record) is True


class TestOversizedAndUnreadableBodies:
    def test_a_body_larger_than_the_limit_is_truncated_not_dropped(
        self, account, client, captured, tracing, settings
    ):
        settings.TRACE_BODY_LIMIT = 200
        client.force_login(account)
        client.post(
            f"/api/2/subscriptions/{USERNAME}/phone.json",
            data=json.dumps({"add": [f"https://example.com/{'a' * 500}.xml"], "remove": []}),
            content_type="application/json",
        )
        assert "truncated" in captured.requests()[-1]["request_body"]

    def test_a_body_the_server_refused_is_reported_not_fatal(
        self, account, client, captured, tracing, settings
    ):
        settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 100
        client.force_login(account)
        response = client.post(
            f"/api/2/episodes/{USERNAME}.json",
            data=json.dumps([{"podcast": "https://e.com/f", "episode": "x" * 500}]),
            content_type="application/json",
        )
        assert response.status_code == 400
        assert captured.requests()[-1]["request_body"].startswith("[rejected")

    def test_a_body_that_is_not_utf8(self, account, client, captured, tracing):
        client.force_login(account)
        client.post(
            f"/api/2/episodes/{USERNAME}.json",
            data=b"\xff\xfe\xfd not json",
            content_type="application/json",
        )
        # Recorded, not crashed, and still on one line.
        assert captured.requests()[-1]["request_body"]
        assert captured.raw.isascii()


def test_turning_tracing_on_says_so(captured, settings):
    from gpodsync.middleware import RequestTracing

    settings.TRACE_REQUESTS = True
    RequestTracing(lambda request: None)
    warnings = [line for line in captured.lines if line.get("event") == "tracing_enabled"]
    assert warnings
    assert warnings[0]["level"] == "WARNING"


class TestDecisionLogs:
    """Why the server did what it did, which the response never says."""

    def test_a_refusal_records_its_reason(self, account, client, captured):
        client.post(LOGIN, data="", content_type="plain/text; charset=utf-8")
        refusals = [line for line in captured.lines if line.get("event") == "auth_refused"]
        assert refusals
        assert refusals[-1]["reason"] == "credentials_rejected"

    def test_a_path_naming_another_account_is_distinguished_in_the_log_only(
        self, account, client, captured
    ):
        # The client cannot tell this from any other 401. Whoever is reading the
        # log can, which is the entire point of the asymmetry.
        get_user_model().objects.create_user(username="stranger", password=PASSWORD)
        client.force_login(account)
        response = client.get("/api/2/devices/stranger.json")

        assert response.status_code == 401
        refusals = [line for line in captured.lines if line.get("event") == "auth_refused"]
        assert refusals[-1]["reason"] == "username_mismatch"

    def test_an_upload_records_how_much_of_it_was_already_known(self, account, client, captured):
        # The number that explains a retry loop, and it is invisible in the
        # response the client receives.
        client.force_login(account)
        payload = json.dumps(
            [
                {
                    "podcast": "https://example.com/a.xml",
                    "episode": "https://example.com/ep1.mp3",
                    "action": "play",
                    "timestamp": "2026-08-26T19:10:00",
                }
            ]
        )
        url = f"/api/2/episodes/{USERNAME}.json"
        client.post(url, data=payload, content_type="application/json")
        client.post(url, data=payload, content_type="application/json")

        written = [
            line for line in captured.lines if line.get("event") == "episode_actions_written"
        ]
        assert written[-2]["stored"] == 1
        assert written[-1]["offered"] == 1
        assert written[-1]["stored"] == 0

    def test_an_import_read_and_its_demotions_are_visible_in_the_log(
        self, account, client, captured
    ):
        # A dump arriving demoted must be explicable from the log alone: the
        # read that raised the flag says so, and the write says what it did.
        client.force_login(account)
        url = f"/api/2/episodes/{USERNAME}.json"
        client.get(url, {"since": "0"})
        client.post(
            url,
            data=json.dumps(
                [
                    {
                        "podcast": "https://example.com/a.xml",
                        "episode": "https://example.com/ep1.mp3",
                        "action": "play",
                        "timestamp": "2026-08-27T09:00:00",
                    }
                ]
            ),
            content_type="application/json",
        )

        reads = [line for line in captured.lines if line.get("event") == "episode_actions_read"]
        assert reads[-1]["initial_import"] is True
        written = [
            line for line in captured.lines if line.get("event") == "episode_actions_written"
        ]
        assert written[-1]["imported"] is True
        assert written[-1]["demoted"] == 1

    def test_a_read_records_the_cursor_it_answered_with(self, account, client, captured):
        client.force_login(account)
        client.get(f"/api/2/subscriptions/{USERNAME}/phone.json?since=0")
        reads = [line for line in captured.lines if line.get("event") == "subscriptions_read"]
        assert reads[-1]["since"] == 0
        assert "cursor" in reads[-1]
