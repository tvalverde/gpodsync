"""Brute-force lockout, and the property that keeps it from becoming the attack.

Every endpoint accepts Basic auth, not just this one, so each is a password
oracle; and with a single account the username is not a secret — it is in the
path of every request and in the proxy's access log.
"""

from base64 import b64encode

import pytest
from django.contrib.auth import get_user_model

pytestmark = [pytest.mark.component, pytest.mark.django_db]

USERNAME = "toni"
PASSWORD = "a-sufficiently-long-passphrase"
LOGIN = f"/api/2/auth/{USERNAME}/login.json"
FAILURE_LIMIT = 3


@pytest.fixture(autouse=True)
def small_limit(settings):
    settings.AXES_FAILURE_LIMIT = FAILURE_LIMIT
    settings.AXES_RESET_ON_SUCCESS = True


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username=USERNAME, password=PASSWORD)


def attempt(client, *, username=USERNAME, password="wrong", address="203.0.113.10"):
    return client.post(
        f"/api/2/auth/{username}/login.json",
        data="",
        content_type="plain/text; charset=utf-8",
        headers={"authorization": "Basic " + b64encode(f"{username}:{password}".encode()).decode()},
        REMOTE_ADDR=address,
    )


def exhaust(client, **kwargs):
    for _ in range(FAILURE_LIMIT):
        attempt(client, **kwargs)


def test_repeated_failures_are_eventually_refused(account, client):
    exhaust(client)
    assert attempt(client).status_code == 429


def test_the_correct_password_is_refused_once_locked(account, client):
    exhaust(client)
    assert attempt(client, password=PASSWORD).status_code == 429


def test_the_owner_can_still_log_in_from_elsewhere(account, client):
    """The property that matters, and the reason the key is not the username.

    Locking on the username alone would mean anyone who knows it — which is
    anyone who has seen a URL — could lock the real owner out from anywhere, by
    guessing badly on purpose. The defence would have become the attack.
    """
    exhaust(client, address="198.51.100.66")
    assert attempt(client, password=PASSWORD, address="203.0.113.10").status_code == 200


def test_a_different_account_from_the_same_address_is_unaffected(account, client):
    # Nor is the key the address alone: one bad actor behind a shared NAT should
    # not lock out everybody who happens to share their exit address.
    get_user_model().objects.create_user(username="other", password=PASSWORD)
    exhaust(client)
    assert attempt(client, username="other", password=PASSWORD).status_code == 200


def test_success_clears_the_count(account, client):
    for _ in range(FAILURE_LIMIT - 1):
        attempt(client)
    assert attempt(client, password=PASSWORD).status_code == 200
    for _ in range(FAILURE_LIMIT - 1):
        attempt(client)
    assert attempt(client, password=PASSWORD).status_code == 200


class TestBehindAReverseProxy:
    """The topology this server is actually deployed in.

    Every request arrives from the proxy, so REMOTE_ADDR is the same for
    everybody and the real client is in X-Forwarded-For. Before the address was
    resolved deliberately, django-axes fell back to REMOTE_ADDR — silently, since
    the proxy settings it was given belonged to a package that was not installed
    — and the lockout key became (username, the proxy). One attacker then locked
    out the account's owner, which is exactly what keying on the address is meant
    to prevent.
    """

    @pytest.fixture(autouse=True)
    def behind_proxy(self, settings):
        settings.TRUSTED_PROXY_HOPS = 1

    def through_proxy(self, client, *, forwarded, password="wrong"):
        return client.post(
            LOGIN,
            data="",
            content_type="plain/text; charset=utf-8",
            headers={
                "authorization": "Basic " + b64encode(f"{USERNAME}:{password}".encode()).decode(),
                "x-forwarded-for": forwarded,
            },
            REMOTE_ADDR="10.0.0.1",
        )

    def test_the_owner_can_still_log_in_while_an_attacker_is_locked(self, account, client):
        for _ in range(FAILURE_LIMIT):
            self.through_proxy(client, forwarded="198.51.100.66")
        assert self.through_proxy(client, forwarded="198.51.100.66").status_code == 429
        assert (
            self.through_proxy(client, forwarded="203.0.113.10", password=PASSWORD).status_code
            == 200
        )

    def test_an_attacker_cannot_prepend_a_fresh_address_each_time(self, account, client):
        # The client's own entries sit to the left of what the proxy appended, so
        # rewriting them changes nothing about which key the attempt is counted
        # under.
        for attempt in range(FAILURE_LIMIT):
            self.through_proxy(client, forwarded=f"1.1.1.{attempt}, 198.51.100.66")
        assert self.through_proxy(client, forwarded="9.9.9.9, 198.51.100.66").status_code == 429


def test_a_nonexistent_account_is_locked_out_the_same_way(client):
    """No oracle in the lockout either.

    If a made-up username never reached 429 — because there is nothing to lock —
    then the difference between 401 forever and 429 eventually would say which
    accounts exist, undoing the identical-401 work one endpoint over.
    """
    for _ in range(FAILURE_LIMIT):
        attempt(client, username="nobody-at-all")
    assert attempt(client, username="nobody-at-all").status_code == 429
