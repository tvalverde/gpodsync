"""The authentication matrix.

The single most important assertion in this file is that four different failures
are indistinguishable. The username appears in the path of every request, so a
server that answers differently for "no such account" than for "wrong password"
publishes its user list to anyone who asks patiently.
"""

from base64 import b64encode

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client

pytestmark = [pytest.mark.component, pytest.mark.django_db]

USERNAME = "toni"
PASSWORD = "a-sufficiently-long-passphrase"
LOGIN = f"/api/2/auth/{USERNAME}/login.json"


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username=USERNAME, password=PASSWORD)


def basic(username: str, password: str) -> str:
    return "Basic " + b64encode(f"{username}:{password}".encode()).decode()


def login(client: Client, username: str = USERNAME, password: str = PASSWORD, path: str = LOGIN):
    return client.post(
        path,
        data="",
        content_type="plain/text; charset=utf-8",
        headers={"authorization": basic(username, password)},
    )


class TestLogin:
    def test_correct_credentials_are_accepted(self, account, client):
        assert login(client).status_code == 200

    def test_the_session_cookie_is_set(self, account, client):
        response = login(client)
        assert "sessionid" in response.cookies

    def test_the_cookie_carries_no_domain(self, account, client):
        # The failure this project exists to fix. A Domain attribute that does not
        # name the host exactly is discarded by AntennaPod's jar without a word,
        # and every later request comes back 401.
        cookie = login(client).cookies["sessionid"]
        assert cookie["domain"] == ""

    def test_the_cookie_is_httponly_and_samesite(self, account, client):
        cookie = login(client).cookies["sessionid"]
        assert cookie["httponly"]
        assert cookie["samesite"] == "Lax"

    def test_the_session_key_is_rotated(self, account, client):
        """Session fixation: an identifier planted before login must not survive it.

        The attack is to hand someone a session id you already know and wait for
        them to authenticate with it. So the scenario is an *anonymous* session,
        which is what Django rotates; logging in twice as the same account
        deliberately keeps its key, and a test built on that would fail while
        proving nothing.

        The first version of this test fetched /healthz/, which touches no
        session, so there was never a cookie to compare — and the assertion's
        `or` made it true without comparing anything. It would have passed
        against a no-op.
        """
        planted = SessionStore()
        planted["attacker_was_here"] = True
        planted.create()

        client.cookies["sessionid"] = planted.session_key
        issued = login(client).cookies["sessionid"].value

        assert issued != planted.session_key

    def test_the_login_response_never_redirects(self, account, client):
        # A same-origin redirect makes OkHttp rewrite this POST into a GET; a
        # cross-scheme one strips the credentials. Both read as a bad password.
        assert login(client).status_code < 300

    def test_the_odd_content_type_is_accepted(self, account, client):
        # plain/text is not a real media type. A framework that negotiates
        # content types rejects this before any application code runs, which is
        # why there is no DRF in this project.
        assert login(client).status_code == 200


class TestIndistinguishableFailures:
    """Four different reasons, one identical answer."""

    def responses(self, client, account):
        return {
            "unknown user": client.post(
                "/api/2/auth/ghost/login.json",
                data="",
                content_type="plain/text; charset=utf-8",
                headers={"authorization": basic("ghost", PASSWORD)},
            ),
            "wrong password": login(client, password="not-the-password"),
            "no credentials at all": client.post(
                LOGIN, data="", content_type="plain/text; charset=utf-8"
            ),
            "url names another account": client.post(
                f"/api/2/auth/{USERNAME}/login.json",
                data="",
                content_type="plain/text; charset=utf-8",
                headers={"authorization": basic("someone-else", PASSWORD)},
            ),
        }

    def test_every_failure_is_401(self, account, client):
        for reason, response in self.responses(client, account).items():
            assert response.status_code == 401, reason

    def test_every_failure_has_a_byte_identical_body(self, account, client):
        bodies = {response.content for response in self.responses(client, account).values()}
        assert len(bodies) == 1

    def test_no_failure_sets_a_session(self, account, client):
        for reason, response in self.responses(client, account).items():
            assert "sessionid" not in response.cookies, reason

    def test_no_www_authenticate_header(self, account, client):
        # Correct HTTP would include one. It is omitted deliberately: the client
        # never reads it, and its presence makes a browser that wanders in pop a
        # credential prompt.
        assert "WWW-Authenticate" not in login(client, password="wrong").headers


class TestCrossOriginRejection:
    """What stands in for the CSRF token the client cannot send."""

    def test_a_request_carrying_an_origin_is_refused(self, account, client):
        response = client.post(
            LOGIN,
            data="",
            content_type="plain/text; charset=utf-8",
            headers={"authorization": basic(USERNAME, PASSWORD), "origin": "https://evil.example"},
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("value", ["cross-site", "same-site"])
    def test_a_browsers_cross_site_marker_is_refused(self, account, client, value):
        response = client.post(
            LOGIN,
            data="",
            content_type="plain/text; charset=utf-8",
            headers={"authorization": basic(USERNAME, PASSWORD), "sec-fetch-site": value},
        )
        assert response.status_code == 403

    def test_the_client_sends_neither_and_is_unaffected(self, account, client):
        # AntennaPod is not a browser: it sends no Origin and no Sec-Fetch-Site.
        assert login(client).status_code == 200

    def test_same_origin_is_allowed(self, account, client):
        response = client.post(
            LOGIN,
            data="",
            content_type="plain/text; charset=utf-8",
            headers={"authorization": basic(USERNAME, PASSWORD), "sec-fetch-site": "same-origin"},
        )
        assert response.status_code == 200


class TestNothingAccumulatesPerLogin:
    def test_a_successful_login_leaves_no_access_log_row(self, account):
        # The client logs in at the start of every sync, so an access log that
        # keeps one row per success grows forever on a server nobody prunes.
        # The failed-attempts table — the actual defence — is separate and
        # untouched by this.
        from axes.models import AccessLog

        client = Client()
        response = client.post(
            LOGIN,
            data="",
            content_type="plain/text; charset=utf-8",
            headers={
                "authorization": "Basic " + b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
            },
        )
        assert response.status_code == 200
        assert AccessLog.objects.count() == 0
