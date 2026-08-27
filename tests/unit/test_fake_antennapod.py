"""Tests for the replica itself.

A test double that is wrong is worse than none: it grants confidence it has not
earned. These assert that the fake is strict in the places the real client is
strict, so that a green acceptance suite means something.
"""

import json

import pytest

from tests.fake_antennapod.client import BATCH_SIZE, FakeAntennaPod, Response
from tests.fake_antennapod.cookies import RejectedCookie, StrictCookieJar
from tests.fake_antennapod.errors import MalformedResponse, SyncFailed, Unauthorised

pytestmark = pytest.mark.unit

HOST = "https://gpodder.example.com"


class ScriptedTransport:
    """Replies from a script and records what it was asked."""

    def __init__(self, *replies: Response) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict, str | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self.replies.pop(0) if self.replies else ok_json({})


def ok_json(payload, *, set_cookie: str | None = None, status: int = 200) -> Response:
    headers = [("Content-Type", "application/json")]
    if set_cookie is not None:
        headers.append(("Set-Cookie", set_cookie))
    return Response(status=status, headers=tuple(headers), body=json.dumps(payload))


def client_for(*replies: Response) -> tuple[FakeAntennaPod, ScriptedTransport]:
    transport = ScriptedTransport(*replies)
    return FakeAntennaPod(base_url=HOST, transport=transport), transport


class TestCredentialsReachOnlyLogin:
    def test_login_sends_basic_auth_with_the_clients_odd_content_type(self):
        client, transport = client_for(ok_json({}, set_cookie="sessionid=abc; Path=/"))
        client.login("toni", "hunter2")

        method, url, headers, body = transport.calls[0]
        assert method == "POST"
        assert url == f"{HOST}/api/2/auth/toni/login.json"
        assert headers["Authorization"].startswith("Basic ")
        assert headers["Content-Type"] == "plain/text; charset=utf-8"
        assert body == ""

    def test_nothing_after_login_carries_credentials(self):
        # The whole reason the cookie has to be right. If this ever passed while
        # sending Authorization, the server could be broken in the exact way this
        # project exists to fix and the suite would not notice.
        client, transport = client_for(
            ok_json({}, set_cookie="sessionid=abc; Path=/"),
            ok_json([]),
        )
        client.login("toni", "hunter2")
        client.get_devices("toni")

        assert "Authorization" not in transport.calls[1][2]
        assert client.requests_carrying_authorization == 1

    def test_the_session_cookie_is_sent_instead(self):
        client, transport = client_for(
            ok_json({}, set_cookie="sessionid=abc; Path=/"),
            ok_json([]),
        )
        client.login("toni", "hunter2")
        client.get_devices("toni")

        assert transport.calls[1][2]["Cookie"] == "sessionid=abc"


class TestCookiePolicy:
    def test_a_host_only_cookie_is_kept(self):
        jar = StrictCookieJar()
        jar.store("sessionid=abc; Path=/; HttpOnly", f"{HOST}/api/2/auth/toni/login.json")
        assert jar.header_for(f"{HOST}/api/2/devices/toni.json") == "sessionid=abc"

    def test_a_mismatched_domain_is_refused(self):
        # ACCEPT_ORIGINAL_SERVER discards this silently on a real phone, which is
        # the single hardest failure in this protocol to diagnose.
        jar = StrictCookieJar()
        with pytest.raises(RejectedCookie, match="Domain"):
            jar.store("sessionid=abc; Domain=.example.com", f"{HOST}/api/2/auth/toni/login.json")

    def test_a_secure_cookie_is_withheld_over_plain_http(self):
        jar = StrictCookieJar()
        jar.store("sessionid=abc; Path=/; Secure", "http://pi.local/api/2/auth/toni/login.json")
        assert jar.header_for("http://pi.local/api/2/devices/toni.json") is None
        assert jar.withheld_because_insecure("http://pi.local/x") == ["sessionid"]

    def test_a_secure_cookie_travels_over_https(self):
        jar = StrictCookieJar()
        jar.store("sessionid=abc; Path=/; Secure", f"{HOST}/api/2/auth/toni/login.json")
        assert jar.header_for(f"{HOST}/api/2/devices/toni.json") == "sessionid=abc"

    def test_a_path_that_does_not_match_is_not_sent(self):
        jar = StrictCookieJar()
        jar.store("sessionid=abc; Path=/admin", f"{HOST}/admin/")
        assert jar.header_for(f"{HOST}/api/2/devices/toni.json") is None

    def test_a_malformed_set_cookie_is_refused(self):
        jar = StrictCookieJar()
        with pytest.raises(RejectedCookie, match="name=value"):
            jar.store("nonsense", HOST)

    def test_the_secure_over_http_failure_is_explained_not_just_reported(self):
        client, _ = client_for(
            ok_json({}, set_cookie="sessionid=abc; Path=/; Secure"),
            Response(status=401, headers=(), body=""),
        )
        client.base_url = "http://pi.local"
        client.login("toni", "hunter2")
        with pytest.raises(Unauthorised, match="marked Secure"):
            client.get_devices("toni")

    def test_a_401_with_no_cookie_at_all_says_so(self):
        client, _ = client_for(ok_json({}), Response(status=401, headers=(), body=""))
        client.login("toni", "hunter2")
        with pytest.raises(Unauthorised, match="no cookie at all"):
            client.get_devices("toni")


class TestMandatoryFields:
    """Fields the Java reads with getJSONArray or getLong. Absent means crash."""

    def test_subscription_changes_need_update_urls(self):
        client, _ = client_for(ok_json({"timestamp": 1}))
        with pytest.raises(MalformedResponse, match="update_urls"):
            client.upload_subscription_changes("toni", "phone", ["https://e.com/f"], [])

    def test_episode_upload_needs_update_urls(self):
        client, _ = client_for(ok_json({"timestamp": 1}))
        with pytest.raises(MalformedResponse, match="update_urls"):
            client.upload_episode_actions("toni", [{"podcast": "p"}])

    @pytest.mark.parametrize("missing", ["add", "remove", "timestamp"])
    def test_subscription_download_needs_every_field(self, missing):
        payload = {"add": [], "remove": [], "timestamp": 1}
        del payload[missing]
        client, _ = client_for(ok_json(payload))
        with pytest.raises(MalformedResponse, match=missing):
            client.get_subscription_changes("toni", "phone", 0)

    @pytest.mark.parametrize("missing", ["actions", "timestamp"])
    def test_episode_download_needs_every_field(self, missing):
        payload = {"actions": [], "timestamp": 1}
        del payload[missing]
        client, _ = client_for(ok_json(payload))
        with pytest.raises(MalformedResponse, match=missing):
            client.get_episode_actions("toni", 0)

    def test_a_timestamp_must_be_a_number(self):
        client, _ = client_for(ok_json({"actions": [], "timestamp": "soon"}))
        with pytest.raises(MalformedResponse, match="whole number"):
            client.get_episode_actions("toni", 0)

    def test_an_array_field_must_be_an_array(self):
        client, _ = client_for(ok_json({"actions": {}, "timestamp": 1}))
        with pytest.raises(MalformedResponse, match="must be a JSON array"):
            client.get_episode_actions("toni", 0)

    def test_a_body_that_is_not_json(self):
        client, _ = client_for(Response(status=200, headers=(), body="<html>oops</html>"))
        with pytest.raises(MalformedResponse, match="not JSON"):
            client.get_episode_actions("toni", 0)


class TestDeviceList:
    def test_reads_a_well_formed_list(self):
        client, _ = client_for(
            ok_json([{"id": "phone", "caption": "Phone", "type": "mobile", "subscriptions": 3}])
        )
        assert client.get_devices("toni")[0]["subscriptions"] == 3

    @pytest.mark.parametrize("missing", ["id", "caption", "type"])
    def test_every_device_field_is_mandatory(self, missing):
        device = {"id": "phone", "caption": "Phone", "type": "mobile", "subscriptions": 3}
        del device[missing]
        client, _ = client_for(ok_json([device]))
        with pytest.raises(MalformedResponse, match=missing):
            client.get_devices("toni")

    @pytest.mark.parametrize("value", [None, "3", True])
    def test_subscriptions_must_be_a_whole_number(self, value):
        client, _ = client_for(
            ok_json([{"id": "p", "caption": "P", "type": "mobile", "subscriptions": value}])
        )
        with pytest.raises(MalformedResponse, match="whole number"):
            client.get_devices("toni")

    def test_the_list_must_be_an_array(self):
        client, _ = client_for(ok_json({"devices": []}))
        with pytest.raises(MalformedResponse, match="JSON array"):
            client.get_devices("toni")

    def test_a_device_must_be_an_object(self):
        client, _ = client_for(ok_json(["phone"]))
        with pytest.raises(MalformedResponse, match="JSON object"):
            client.get_devices("toni")


class TestStatusCodes:
    """The client requires exactly 200. Anything else is a failed sync."""

    @pytest.mark.parametrize("status", [201, 202, 204, 206, 300, 304])
    def test_a_non_200_success_code_is_still_a_failure(self, status):
        # 201 and 204 are natural choices for a POST endpoint, and a lenient
        # fake would let a server that returned one ship broken to every device.
        client, _ = client_for(Response(status=status, headers=(), body=""))
        with pytest.raises(SyncFailed, match="exactly 200"):
            client.login("toni", "hunter2")

    def test_200_is_accepted(self):
        client, _ = client_for(ok_json({}, set_cookie="sessionid=abc; Path=/"))
        client.login("toni", "hunter2")
        assert len(client.jar) == 1


class TestBatching:
    def test_uploads_thirty_at_a_time(self):
        actions = [{"podcast": f"https://e.com/{n}"} for n in range(65)]
        replies = [ok_json({"timestamp": n, "update_urls": []}) for n in range(3)]
        client, transport = client_for(*replies)

        results = client.upload_episode_actions("toni", actions)

        assert len(transport.calls) == 3
        assert [len(json.loads(call[3])) for call in transport.calls] == [30, 30, 5]
        assert len(results) == 3

    def test_an_exact_multiple_does_not_send_an_empty_batch(self):
        actions = [{"podcast": f"https://e.com/{n}"} for n in range(BATCH_SIZE)]
        client, transport = client_for(ok_json({"timestamp": 1, "update_urls": []}))
        client.upload_episode_actions("toni", actions)
        assert len(transport.calls) == 1

    def test_nothing_to_upload_sends_nothing(self):
        client, transport = client_for()
        assert client.upload_episode_actions("toni", []) == []
        assert transport.calls == []


class TestRedirects:
    """Two mechanisms, one symptom. Both are why no endpoint may return 3xx."""

    def test_a_same_origin_redirect_keeps_credentials_but_rewrites_the_method(self):
        # What APPEND_SLASH would do. The header survives — the folklore that it
        # does not is wrong — but OkHttp turns the POST into a GET, so login is
        # asked for a method it does not answer.
        client, transport = client_for(
            Response(status=301, headers=(("Location", "/api/2/auth/toni/login.json/"),), body=""),
            Response(status=401, headers=(), body=""),
        )
        with pytest.raises(Unauthorised):
            client.login("toni", "hunter2")

        assert client.redirects_followed == 1
        assert client.methods_rewritten_by_redirect == 1
        assert client.credentials_dropped_by_redirect == 0
        assert transport.calls[0][0] == "POST"
        assert transport.calls[1][0] == "GET"
        assert "Authorization" in transport.calls[1][2]

    def test_a_cross_scheme_redirect_drops_the_credentials(self):
        # What SECURE_SSL_REDIRECT would do. Here the header really is stripped,
        # because the connection cannot be reused.
        client, transport = client_for(
            Response(
                status=307,
                headers=(("Location", "https://gpodder.example.com/api/2/auth/toni/login.json"),),
                body="",
            ),
            Response(status=401, headers=(), body=""),
        )
        client.base_url = "http://gpodder.example.com"
        with pytest.raises(Unauthorised):
            client.login("toni", "hunter2")

        assert client.credentials_dropped_by_redirect == 1
        assert "Authorization" not in transport.calls[1][2]

    def test_making_a_default_port_explicit_is_not_a_scheme_change(self):
        # Same origin, spelled differently. Comparing ports raw made this look
        # like a cross-origin hop and dropped credentials the real client keeps.
        client, transport = client_for(
            Response(
                status=307,
                headers=(
                    ("Location", "https://gpodder.example.com:443/api/2/auth/toni/login.json"),
                ),
                body="",
            ),
            ok_json({}, set_cookie="sessionid=abc; Path=/"),
        )
        client.login("toni", "hunter2")
        assert client.credentials_dropped_by_redirect == 0
        assert "Authorization" in transport.calls[1][2]

    def test_307_preserves_the_method(self):
        client, transport = client_for(
            Response(status=307, headers=(("Location", "/api/2/auth/toni/login.json/"),), body=""),
            ok_json({}, set_cookie="sessionid=abc; Path=/"),
        )
        client.login("toni", "hunter2")
        assert transport.calls[1][0] == "POST"
        assert client.methods_rewritten_by_redirect == 0
