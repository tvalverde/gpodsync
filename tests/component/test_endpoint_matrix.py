"""Endpoint by status code, and the bounds that keep a request finite.

The status assertions matter more than they look: the client throws on anything
that is not exactly 200, so a 201 for a POST that creates a device would be a
failed sync rather than a tidier answer.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = [pytest.mark.component, pytest.mark.django_db]

USERNAME = "toni"
PASSWORD = "a-sufficiently-long-passphrase"
FEED = "https://example.com/a.xml"

DEVICES = f"/api/2/devices/{USERNAME}.json"
DEVICE = f"/api/2/devices/{USERNAME}/phone.json"
SUBSCRIPTIONS = f"/api/2/subscriptions/{USERNAME}/phone.json"
EPISODES = f"/api/2/episodes/{USERNAME}.json"


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username=USERNAME, password=PASSWORD)


@pytest.fixture
def session(account):
    client = Client()
    # force_login rather than login: the test client's login() calls
    # authenticate() with no request, which axes correctly refuses. Establishing
    # the session is scaffolding here; authentication has its own suite.
    client.force_login(account)
    return client


def action(**overrides):
    return {
        "podcast": FEED,
        "episode": "https://example.com/ep1.mp3",
        "action": "play",
        "timestamp": "2026-08-26T19:10:00",
    } | overrides


class TestEveryEndpointNeedsAuthentication:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", DEVICES),
            ("post", DEVICE),
            ("get", SUBSCRIPTIONS),
            ("post", SUBSCRIPTIONS),
            ("get", EPISODES),
            ("post", EPISODES),
        ],
    )
    def test_without_a_session_the_answer_is_401(self, account, client, method, path):
        response = client.generic(method.upper(), path, data=b"{}", content_type="application/json")
        assert response.status_code == 401

    @pytest.mark.parametrize("path", [DEVICES, EPISODES])
    def test_a_session_cannot_reach_another_account(self, session, path):
        get_user_model().objects.create_user(username="stranger", password=PASSWORD)
        assert session.get(path.replace(USERNAME, "stranger")).status_code == 401


class TestSuccessIsAlwaysExactly200:
    def test_device_list(self, session):
        assert session.get(DEVICES).status_code == 200

    def test_device_configuration_is_200_not_201(self, session):
        # It creates a device. 201 Created is the natural REST answer and would
        # be a failed sync on the phone.
        response = session.post(
            DEVICE,
            data=json.dumps({"caption": "P", "type": "mobile"}),
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_subscription_upload_is_200_not_204(self, session):
        response = session.post(
            SUBSCRIPTIONS,
            data=json.dumps({"add": [FEED], "remove": []}),
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_episode_upload(self, session):
        response = session.post(
            EPISODES, data=json.dumps([action()]), content_type="application/json"
        )
        assert response.status_code == 200


class TestMandatoryFieldsArePresentEvenWhenEmpty:
    def test_subscription_download(self, session):
        body = session.get(f"{SUBSCRIPTIONS}?since=0").json()
        assert set(body) >= {"add", "remove", "timestamp"}

    def test_subscription_upload_always_carries_update_urls(self, session):
        body = session.post(
            SUBSCRIPTIONS,
            data=json.dumps({"add": [], "remove": []}),
            content_type="application/json",
        ).json()
        assert body["update_urls"] == []
        assert isinstance(body["timestamp"], int)

    def test_episode_download(self, session):
        body = session.get(f"{EPISODES}?since=0").json()
        assert set(body) >= {"actions", "timestamp"}

    def test_episode_upload_always_carries_update_urls(self, session):
        body = session.post(EPISODES, data=json.dumps([]), content_type="application/json").json()
        assert body["update_urls"] == []
        assert isinstance(body["timestamp"], int)


class TestNothingRedirects:
    @pytest.mark.parametrize(
        "path",
        [DEVICES, DEVICE, SUBSCRIPTIONS, EPISODES, f"{EPISODES}?since=0", "/api/2/devices/x.json"],
    )
    def test_no_api_path_answers_with_a_redirect(self, session, path):
        # Including the trailing-slash forms APPEND_SLASH would otherwise create.
        for candidate in (path, path.replace(".json", ".json/")):
            assert (
                session.get(candidate).status_code < 300
                or session.get(candidate).status_code >= 400
            )


class TestMalformedInput:
    def test_a_body_that_is_not_json(self, session):
        assert (
            session.post(
                SUBSCRIPTIONS, data="not json", content_type="application/json"
            ).status_code
            == 400
        )

    def test_a_subscription_body_that_is_not_an_object(self, session):
        assert (
            session.post(SUBSCRIPTIONS, data="[]", content_type="application/json").status_code
            == 400
        )

    def test_an_episode_body_that_is_not_an_array(self, session):
        assert session.post(EPISODES, data="{}", content_type="application/json").status_code == 400

    def test_add_and_remove_must_be_arrays(self, session):
        assert (
            session.post(
                SUBSCRIPTIONS,
                data=json.dumps({"add": "a-string"}),
                content_type="application/json",
            ).status_code
            == 400
        )

    def test_a_feed_url_that_will_not_be_stored(self, session):
        assert (
            session.post(
                SUBSCRIPTIONS,
                data=json.dumps({"add": ["javascript:alert(1)"], "remove": []}),
                content_type="application/json",
            ).status_code
            == 400
        )

    def test_an_action_missing_a_mandatory_field(self, session):
        broken = action()
        del broken["podcast"]
        assert (
            session.post(
                EPISODES, data=json.dumps([broken]), content_type="application/json"
            ).status_code
            == 400
        )

    @pytest.mark.parametrize("value", ["abc", "-1", "1.5"])
    def test_an_unreadable_cursor_is_reported_not_ignored(self, session, value):
        # Treating it as zero would silently resend the entire history.
        assert session.get(f"{EPISODES}?since={value}").status_code == 400

    def test_an_absent_cursor_means_everything(self, session):
        assert session.get(EPISODES).status_code == 200

    def test_a_device_name_that_would_need_escaping(self, session):
        assert session.post(
            f"/api/2/subscriptions/{USERNAME}/has%20space.json",
            data=json.dumps({"add": [], "remove": []}),
            content_type="application/json",
        ).status_code in (400, 404)


class TestBounds:
    def test_too_many_urls_in_one_request(self, session):
        response = session.post(
            SUBSCRIPTIONS,
            data=json.dumps({"add": [f"https://e.com/{n}.xml" for n in range(5001)], "remove": []}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_too_many_actions_in_one_request(self, session):
        response = session.post(
            EPISODES,
            data=json.dumps([action(episode=f"https://e.com/{n}.mp3") for n in range(1001)]),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_an_oversized_body_is_refused(self, session):
        response = session.post(
            EPISODES,
            data=json.dumps([action(guid="x" * 2_000_000)]),
            content_type="application/json",
        )
        assert response.status_code == 400


class TestValuesThatUsedToGetThrough:
    def test_a_guid_longer_than_the_column(self, session):
        # SQLite does not enforce VARCHAR lengths and bulk_create runs no
        # validators, so the declared 255 was a promise nothing kept.
        assert (
            session.post(
                EPISODES,
                data=json.dumps([action(guid="x" * 256)]),
                content_type="application/json",
            ).status_code
            == 400
        )

    @pytest.mark.parametrize("value", [2**63, 10**20, -(2**40)])
    def test_a_playback_offset_out_of_range(self, session, value):
        # Past SQLite's integer range this raised OverflowError and reached the
        # client as a 500.
        assert (
            session.post(
                EPISODES,
                data=json.dumps([action(started=0, position=value, total=500)]),
                content_type="application/json",
            ).status_code
            == 400
        )

    # The Arabic-Indic digit is escaped so the file stays ASCII; int() reads it
    # as five, which is the whole point of rejecting it.
    @pytest.mark.parametrize("value", ["1_000", "+5", " 5 ", "\u0665", str(2**63)])
    def test_a_cursor_that_is_not_plain_digits(self, session, value):
        # int() reads all of these as numbers the sender did not write, and the
        # last would be echoed back as something the client's getLong cannot
        # parse — after which it would never sync again.
        assert session.get(f"{EPISODES}?since={value}").status_code == 400

    def test_an_array_entry_that_is_not_an_object(self, session):
        assert (
            session.post(
                EPISODES,
                data=json.dumps([action(), "garbage", 42]),
                content_type="application/json",
            ).status_code
            == 400
        )


class TestCrossAccountWrites:
    @pytest.mark.parametrize(
        ("path", "body"),
        [
            (SUBSCRIPTIONS, {"add": [FEED], "remove": []}),
            (EPISODES, [{"podcast": FEED, "episode": "https://e.com/e", "action": "new"}]),
        ],
    )
    def test_a_session_cannot_write_to_another_account(self, session, path, body):
        from gpodsync.models import EpisodeActionRecord, SubscriptionChange

        get_user_model().objects.create_user(username="stranger", password=PASSWORD)
        response = session.post(
            path.replace(USERNAME, "stranger"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert response.status_code == 401
        assert SubscriptionChange.objects.count() == 0
        assert EpisodeActionRecord.objects.count() == 0
