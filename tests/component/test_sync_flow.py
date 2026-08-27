"""The pedantic client against the real application, in process.

Everything here is asserted by the replica rather than by the test: if the server
omits `update_urls`, returns 201 instead of 200, or sets a cookie the Java jar
would discard, the client raises before any assertion in this file runs. That is
the point of having written it first.
"""

import base64
import json
from datetime import UTC, datetime

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from gpodsync.models import EpisodeActionRecord
from tests.fake_antennapod.client import FakeAntennaPod
from tests.fake_antennapod.django_transport import DjangoTestClientTransport
from tests.fake_antennapod.errors import Unauthorised

pytestmark = [pytest.mark.component, pytest.mark.django_db]

USERNAME = "toni"
PASSWORD = "a-sufficiently-long-passphrase"
FEED_A = "https://example.com/a.xml"
FEED_B = "https://example.com/b.xml"


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username=USERNAME, password=PASSWORD)


@pytest.fixture
def phone(account):
    client = FakeAntennaPod(
        base_url="http://testserver", transport=DjangoTestClientTransport(Client())
    )
    client.login(USERNAME, PASSWORD)
    return client


def test_login_leaves_the_client_holding_a_usable_cookie(phone):
    # If the cookie carried a Domain, the strict jar would have refused it during
    # login and this would fail with "no cookie at all".
    assert len(phone.jar) == 1
    assert phone.get_devices(USERNAME) == []


def test_no_request_after_login_carries_credentials(phone):
    phone.get_devices(USERNAME)
    phone.get_subscription_changes(USERNAME, "phone", 0)
    assert phone.requests_carrying_authorization == 1


def test_nothing_ever_redirects(phone):
    phone.get_devices(USERNAME)
    phone.configure_device(USERNAME, "phone", "Phone", "mobile")
    phone.get_episode_actions(USERNAME, 0)
    assert phone.redirects_followed == 0


class TestDevices:
    def test_a_configured_device_appears_in_the_list(self, phone):
        phone.configure_device(USERNAME, "phone", "My Phone", "mobile")
        devices = phone.get_devices(USERNAME)
        assert devices == [
            {"id": "phone", "caption": "My Phone", "type": "mobile", "subscriptions": 0}
        ]

    def test_the_subscription_count_is_a_number_and_is_right(self, phone):
        phone.configure_device(USERNAME, "phone", "My Phone", "mobile")
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A, FEED_B], [])
        assert phone.get_devices(USERNAME)[0]["subscriptions"] == 2

    def test_syncing_a_device_that_was_never_configured_still_works(self, phone):
        # The client configures and syncs in whichever order it likes; refusing
        # the second would be a sync that never starts.
        phone.upload_subscription_changes(USERNAME, "unconfigured", [FEED_A], [])
        assert [d["id"] for d in phone.get_devices(USERNAME)] == ["unconfigured"]


class TestSubscriptions:
    def test_a_subscription_survives_the_round_trip(self, phone):
        _, update_urls = phone.upload_subscription_changes(USERNAME, "phone", [FEED_A], [])
        assert update_urls == []
        added, removed, _ = phone.get_subscription_changes(USERNAME, "phone", 0)
        assert added == [FEED_A]
        assert removed == []

    def test_the_cursor_delivers_each_change_once(self, phone):
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A], [])
        _, _, cursor = phone.get_subscription_changes(USERNAME, "phone", 0)

        added, removed, next_cursor = phone.get_subscription_changes(USERNAME, "phone", cursor)
        assert (added, removed) == ([], [])
        assert next_cursor >= cursor

        phone.upload_subscription_changes(USERNAME, "phone", [FEED_B], [])
        added, _, _ = phone.get_subscription_changes(USERNAME, "phone", cursor)
        assert added == [FEED_B]

    def test_unsubscribing_is_reported_as_a_removal(self, phone):
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A], [])
        _, _, cursor = phone.get_subscription_changes(USERNAME, "phone", 0)
        phone.upload_subscription_changes(USERNAME, "phone", [], [FEED_A])

        added, removed, _ = phone.get_subscription_changes(USERNAME, "phone", cursor)
        assert added == []
        assert removed == [FEED_A]

    def test_two_devices_share_one_list(self, phone):
        # Asserted the opposite until the case this server exists for was tried:
        # a second phone starting empty received nothing and stayed empty.
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A], [])
        phone.upload_subscription_changes(USERNAME, "tablet", [FEED_B], [])
        added, _, _ = phone.get_subscription_changes(USERNAME, "tablet", 0)
        assert sorted(added) == sorted([FEED_A, FEED_B])

    def test_a_url_needing_normalisation_comes_back_as_an_update_pair(self, phone):
        _, update_urls = phone.upload_subscription_changes(
            USERNAME, "phone", ["HTTPS://Example.com/a.xml"], []
        )
        assert update_urls == [["HTTPS://Example.com/a.xml", "https://example.com/a.xml"]]


class TestEpisodeActions:
    def action(self, position=120):
        return {
            "podcast": FEED_A,
            "episode": "https://example.com/ep1.mp3",
            "action": "play",
            "timestamp": "2026-08-26T19:10:00",
            "started": 0,
            "position": position,
            "total": 500,
            "device": "phone",
        }

    def test_an_action_survives_the_round_trip(self, phone):
        phone.upload_episode_actions(USERNAME, [self.action()])
        actions, _ = phone.get_episode_actions(USERNAME, 0)
        assert len(actions) == 1
        assert actions[0]["position"] == 120
        assert actions[0]["timestamp"] == "2026-08-26T19:10:00"

    def test_a_position_of_zero_is_not_sent_back_as_data(self, phone):
        # The client would read it as a bare play and discard it, so echoing it
        # would be worse than saying nothing.
        phone.upload_episode_actions(USERNAME, [self.action(position=0)])
        actions, _ = phone.get_episode_actions(USERNAME, 0)
        assert "position" not in actions[0]

    def test_a_full_batch_of_thirty_is_accepted(self, phone):
        actions = [
            {**self.action(), "episode": f"https://example.com/ep{n}.mp3"} for n in range(30)
        ]
        results = phone.upload_episode_actions(USERNAME, actions)
        assert len(results) == 1
        stored, _ = phone.get_episode_actions(USERNAME, 0)
        assert len(stored) == 30

    def test_more_than_one_batch_is_split_and_all_of_it_arrives(self, phone):
        actions = [
            {**self.action(), "episode": f"https://example.com/ep{n}.mp3"} for n in range(65)
        ]
        assert len(phone.upload_episode_actions(USERNAME, actions)) == 3
        stored, _ = phone.get_episode_actions(USERNAME, 0)
        assert len(stored) == 65

    def test_the_cursor_delivers_each_action_once(self, phone):
        phone.upload_episode_actions(USERNAME, [self.action()])
        _, cursor = phone.get_episode_actions(USERNAME, 0)
        actions, _ = phone.get_episode_actions(USERNAME, cursor)
        assert actions == []

    def test_uploading_the_same_action_twice_does_not_duplicate_history(self, phone):
        # This flow's own GET reads from zero, so the second upload arrives
        # demoted to the import sentinel — and survives only because identical
        # content collapses under the constraint. The accidental integration
        # test of demotion and content-keyed idempotency working together.
        # Idempotence as the client experiences it: a retried sync must not make
        # the same moment appear twice.
        phone.upload_episode_actions(USERNAME, [self.action()])
        _, cursor = phone.get_episode_actions(USERNAME, 0)
        phone.upload_episode_actions(USERNAME, [self.action()])
        later, _ = phone.get_episode_actions(USERNAME, cursor)
        assert later == []


class TestAnotherAccount:
    def test_one_account_cannot_read_anothers_data(self, phone):
        get_user_model().objects.create_user(username="stranger", password=PASSWORD)
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A], [])

        intruder = FakeAntennaPod(
            base_url="http://testserver", transport=DjangoTestClientTransport(Client())
        )
        intruder.login("stranger", PASSWORD)
        added, _, _ = intruder.get_subscription_changes("stranger", "phone", 0)
        assert added == []

    def test_a_session_cannot_be_used_against_another_username(self, phone):
        get_user_model().objects.create_user(username="stranger", password=PASSWORD)
        with pytest.raises(Unauthorised):
            phone.get_devices("stranger")


class TestThingsThatWereSilentlyLost:
    """Regressions with a cost measured in somebody's listening history."""

    def moment(self, position):
        return {
            "podcast": FEED_A,
            "episode": "https://example.com/ep1.mp3",
            "action": "play",
            "timestamp": "2026-08-26T19:10:00",
            "started": 0,
            "position": position,
            "total": 500,
        }

    def test_a_new_position_in_the_same_second_is_stored_but_only_the_first_is_served(self, phone):
        # A pause, a seek and another pause inside one second. The uniqueness key
        # did not include the offsets, so the second report was indistinguishable
        # from a duplicate and vanished without an error. Storage keeps both;
        # the wire carries one, because equal stamps tie and the client's own
        # override rule is strictly-after — the first processed action sticks.
        from gpodsync.models import EpisodeActionRecord

        phone.upload_episode_actions(USERNAME, [self.moment(120)])
        phone.upload_episode_actions(USERNAME, [self.moment(450)])

        assert EpisodeActionRecord.objects.count() == 2
        actions, _ = phone.get_episode_actions(USERNAME, 0)
        assert [action["position"] for action in actions] == [120]

    def test_an_identical_retry_still_deduplicates(self, phone):
        phone.upload_episode_actions(USERNAME, [self.moment(120)])
        phone.upload_episode_actions(USERNAME, [self.moment(120)])
        actions, _ = phone.get_episode_actions(USERNAME, 0)
        assert len(actions) == 1

    def test_a_retry_of_a_play_with_no_position_still_deduplicates(self, phone):
        # The offsets are coalesced in the key rather than listed as nullable
        # columns, because SQLite treats NULLs in a unique index as distinct —
        # which would have stopped this case deduplicating at all.
        bare = {
            "podcast": FEED_A,
            "episode": "https://example.com/ep2.mp3",
            "action": "play",
            "timestamp": "2026-08-26T19:10:00",
        }
        phone.upload_episode_actions(USERNAME, [bare])
        phone.upload_episode_actions(USERNAME, [bare])
        actions, _ = phone.get_episode_actions(USERNAME, 0)
        assert len(actions) == 1

    def test_adding_and_removing_the_same_feed_in_one_batch_ends_unsubscribed(self, phone):
        # The two halves used to be compared against one snapshot taken before
        # either was applied, so the removal was dropped and the outcome depended
        # on the previous state rather than on what was sent.
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A], [FEED_A])
        added, _, _ = phone.get_subscription_changes(USERNAME, "phone", 0)
        assert added == []


class TestInitialImportDemotion:
    """A session that read from cursor 0 is about to dump a database stamped
    with the moment of the dump; everything it uploads is stored at the
    sentinel so the dump can fill vacuums but never override real history."""

    EPISODES_URL = f"/api/2/episodes/{USERNAME}.json"
    SENTINEL = datetime(1970, 1, 1, tzinfo=UTC)

    def a_wire_action(self, position=120, episode="https://example.com/ep1.mp3"):
        return {
            "podcast": FEED_A,
            "episode": episode,
            "action": "play",
            "timestamp": "2026-08-27T09:00:00",
            "started": 0,
            "position": position,
            "total": 500,
        }

    def logged_in(self, account):
        client = Client()
        client.force_login(account)
        return client

    def upload(self, client, *actions):
        return client.post(
            self.EPISODES_URL, data=json.dumps(list(actions)), content_type="application/json"
        )

    # -- the flag's lifecycle --

    def test_a_read_from_zero_raises_the_flag(self, account):
        client = self.logged_in(account)
        client.get(self.EPISODES_URL, {"since": "0"})
        assert client.session["initial_import"] is True

    def test_a_read_from_a_real_cursor_lowers_it(self, account):
        client = self.logged_in(account)
        client.get(self.EPISODES_URL, {"since": "0"})
        client.get(self.EPISODES_URL, {"since": "1"})
        assert "initial_import" not in client.session

    def test_a_read_from_a_real_cursor_with_no_flag_is_a_no_op(self, account):
        client = self.logged_in(account)
        response = client.get(self.EPISODES_URL, {"since": "1"})
        assert response.status_code == 200
        assert "initial_import" not in client.session

    def test_a_fresh_session_does_not_carry_the_flag(self, account):
        first = self.logged_in(account)
        first.get(self.EPISODES_URL, {"since": "0"})

        second = self.logged_in(account)
        assert "initial_import" not in second.session

    def test_the_flag_survives_a_relogin_on_the_same_jar(self, account):
        # Django's login() cycles the session key but keeps the session data,
        # so a mid-dump re-login does not lift the demotion; the read that
        # follows it — from a real cursor once the dump completed — is what
        # clears the flag.
        client = self.logged_in(account)
        client.get(self.EPISODES_URL, {"since": "0"})
        client.force_login(account)
        assert client.session["initial_import"] is True

    def test_the_flag_round_trips_through_the_session_backend(self, account):
        # SESSION_SAVE_EVERY_REQUEST is off; assigning the key must mark the
        # session modified or the flag would evaporate with the response.
        client = self.logged_in(account)
        client.get(self.EPISODES_URL, {"since": "0"})

        stranger = Client()
        stranger.cookies = client.cookies
        response = self.upload(stranger, self.a_wire_action())
        assert response.status_code == 200
        assert EpisodeActionRecord.objects.get().happened_at == self.SENTINEL

    # -- what the flag does --

    def test_an_upload_after_a_read_from_zero_is_stored_at_the_sentinel(self, account):
        client = self.logged_in(account)
        client.get(self.EPISODES_URL, {"since": "0"})
        self.upload(client, self.a_wire_action())
        assert EpisodeActionRecord.objects.get().happened_at == self.SENTINEL

    def test_an_upload_without_the_flag_keeps_its_timestamp(self, account):
        client = self.logged_in(account)
        self.upload(client, self.a_wire_action())
        assert EpisodeActionRecord.objects.get().happened_at == datetime(
            2026, 8, 27, 9, 0, tzinfo=UTC
        )

    def test_every_batch_of_a_multi_batch_dump_is_demoted(self, account):
        client = self.logged_in(account)
        client.get(self.EPISODES_URL, {"since": "0"})
        self.upload(client, self.a_wire_action(episode="https://example.com/1.mp3"))
        self.upload(client, self.a_wire_action(episode="https://example.com/2.mp3"))
        stamps = set(EpisodeActionRecord.objects.values_list("happened_at", flat=True))
        assert stamps == {self.SENTINEL}

    def test_an_upload_after_the_cursor_advances_is_not_demoted(self, account):
        client = self.logged_in(account)
        client.get(self.EPISODES_URL, {"since": "0"})
        self.upload(client, self.a_wire_action(episode="https://example.com/1.mp3"))
        cursor = EpisodeActionRecord.cursor_for(user_id=account.id, floor=0)
        client.get(self.EPISODES_URL, {"since": str(cursor)})

        self.upload(client, self.a_wire_action(episode="https://example.com/2.mp3"))
        fresh = EpisodeActionRecord.objects.get(episode="https://example.com/2.mp3")
        assert fresh.happened_at == datetime(2026, 8, 27, 9, 0, tzinfo=UTC)

    def test_a_cookie_less_read_from_zero_mints_no_session(self, account):
        # A cookie-less Basic reader cannot be mid-dump — the client always
        # holds the session cookie after login — so flagging it would only
        # mint an orphan session row per request, a slow leak with no payoff.
        from django.contrib.sessions.models import Session

        credentials = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        response = Client().get(
            self.EPISODES_URL, {"since": "0"}, HTTP_AUTHORIZATION=f"Basic {credentials}"
        )
        assert response.status_code == 200
        assert Session.objects.count() == 0

    def test_a_basic_auth_upload_without_a_cookie_is_not_demoted(self, account):
        # The accepted limitation, as a test rather than folklore: a caller that
        # discards the session cookie cannot carry the flag, and its uploads
        # keep their own stamps. AntennaPod always holds the cookie. Two client
        # instances, because Django's test client keeps cookies — holding the
        # one the GET minted would quietly turn this into the demoted case.
        credentials = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        Client().get(self.EPISODES_URL, {"since": "0"}, HTTP_AUTHORIZATION=f"Basic {credentials}")
        response = Client().post(
            self.EPISODES_URL,
            data=json.dumps([self.a_wire_action()]),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )
        assert response.status_code == 200
        assert EpisodeActionRecord.objects.get().happened_at == datetime(
            2026, 8, 27, 9, 0, tzinfo=UTC
        )
