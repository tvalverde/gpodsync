"""The scenarios a person would actually notice.

Measured by this list rather than by a coverage percentage: gating these on lines
executed would reward tests that touch code over tests that prove a phone syncs.

Everything runs against the published image, in the shape the documentation
recommends — unprivileged, read-only root filesystem, every capability dropped —
so the suite proves the deployment people are told to use.
"""

import pytest

from tests.acceptance.conftest import PASSWORD, USERNAME, device
from tests.fake_antennapod.errors import SyncFailed, Unauthorised

pytestmark = pytest.mark.acceptance

FEED_A = "https://example.com/a.xml"
FEED_B = "https://example.com/b.xml"
EPISODE = "https://example.com/a/ep1.mp3"


def play(position: int, *, episode: str = EPISODE, at: str = "2026-08-26T19:10:00") -> dict:
    return {
        "podcast": FEED_A,
        "episode": episode,
        "action": "play",
        "timestamp": at,
        "started": 0,
        "position": position,
        "total": 1800,
    }


class TestFirstRun:
    def test_a_fresh_container_answers_and_has_an_account(self, phone):
        # Reaching this line means the image started unprivileged on an empty
        # volume, migrated, created the account from the environment, and set a
        # cookie the Java jar was willing to keep.
        assert phone.get_devices(USERNAME) == []

    def test_registering_a_device_and_syncing_it(self, phone):
        phone.configure_device(USERNAME, "phone", "My Phone", "mobile")
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A, FEED_B], [])

        devices = phone.get_devices(USERNAME)
        assert devices == [
            {"id": "phone", "caption": "My Phone", "type": "mobile", "subscriptions": 2}
        ]

    def test_credentials_are_sent_once_and_never_again(self, phone):
        phone.get_devices(USERNAME)
        phone.get_episode_actions(USERNAME, 0)
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A], [])
        assert phone.requests_carrying_authorization == 1

    def test_nothing_redirects(self, phone):
        phone.get_devices(USERNAME)
        phone.configure_device(USERNAME, "phone", "P", "mobile")
        phone.get_subscription_changes(USERNAME, "phone", 0)
        assert phone.redirects_followed == 0


class TestTwoDevices:
    def test_a_subscription_made_on_one_appears_on_the_other(self, phone, tablet):
        """The reason this server exists, against the real image.

        This test was once named for what the code did rather than for what the
        product needed, and it passed. Subscriptions were per device, so a second
        phone with an empty AntennaPod received nothing on its first sync and
        stayed empty — while a green suite said everything worked.
        """
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A, FEED_B], [])

        arriving, removed, cursor = tablet.get_subscription_changes(USERNAME, "tablet", 0)
        assert sorted(arriving) == sorted([FEED_A, FEED_B])
        assert removed == []

        # And it keeps working afterwards, not only on the first sync.
        phone.upload_subscription_changes(USERNAME, "phone", ["https://example.com/c.xml"], [])
        later, _, _ = tablet.get_subscription_changes(USERNAME, "tablet", cursor)
        assert later == ["https://example.com/c.xml"]

    def test_an_unsubscribe_on_one_reaches_the_other(self, phone, tablet):
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A, FEED_B], [])
        _, _, cursor = tablet.get_subscription_changes(USERNAME, "tablet", 0)

        tablet.upload_subscription_changes(USERNAME, "tablet", [], [FEED_A])

        _, removed, _ = phone.get_subscription_changes(USERNAME, "phone", cursor)
        assert removed == [FEED_A]

    def test_a_listening_position_reaches_the_other_device(self, phone, tablet):
        _, cursor = tablet.get_episode_actions(USERNAME, 0)
        phone.upload_episode_actions(USERNAME, [play(600)])

        actions, _ = tablet.get_episode_actions(USERNAME, cursor)
        assert [a["position"] for a in actions] == [600]

    def test_the_later_position_wins_when_listening_continues(self, phone, tablet):
        # Exactly one action crosses the wire: the superseded 600 is suppressed,
        # not merely outranked, because the client applies whatever it receives.
        phone.upload_episode_actions(USERNAME, [play(600)])
        phone.upload_episode_actions(USERNAME, [play(900, at="2026-08-26T19:20:00")])

        actions, _ = tablet.get_episode_actions(USERNAME, 0)
        assert [a["position"] for a in actions] == [900]

    def test_two_positions_in_the_same_second_serve_one_winner(self, phone, tablet):
        # A pause, a seek, another pause. Both are real reports and both are
        # stored (the component suite proves that half); the wire carries the
        # first, mirroring the client's strictly-after override on a tie.
        phone.upload_episode_actions(USERNAME, [play(600)])
        phone.upload_episode_actions(USERNAME, [play(1200)])
        actions, _ = tablet.get_episode_actions(USERNAME, 0)
        assert [a["position"] for a in actions] == [600]


class TestUnsubscribing:
    def test_a_removed_feed_is_reported_as_removed(self, phone):
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A, FEED_B], [])
        _, _, cursor = phone.get_subscription_changes(USERNAME, "phone", 0)

        phone.upload_subscription_changes(USERNAME, "phone", [], [FEED_A])
        added, removed, _ = phone.get_subscription_changes(USERNAME, "phone", cursor)

        assert added == []
        assert removed == [FEED_A]

    def test_the_device_count_follows(self, phone):
        phone.configure_device(USERNAME, "phone", "P", "mobile")
        phone.upload_subscription_changes(USERNAME, "phone", [FEED_A, FEED_B], [])
        phone.upload_subscription_changes(USERNAME, "phone", [], [FEED_A])
        assert phone.get_devices(USERNAME)[0]["subscriptions"] == 1


class TestIncrementalSync:
    def test_each_change_arrives_once_and_only_once(self, phone):
        seen: list[str] = []
        cursor = 0
        for feed in (FEED_A, FEED_B, "https://example.com/c.xml"):
            phone.upload_subscription_changes(USERNAME, "phone", [feed], [])
            added, _, cursor = phone.get_subscription_changes(USERNAME, "phone", cursor)
            seen += added

        assert seen == [FEED_A, FEED_B, "https://example.com/c.xml"]
        added, removed, _ = phone.get_subscription_changes(USERNAME, "phone", cursor)
        assert (added, removed) == ([], [])

    def test_a_full_batch_of_thirty_and_then_some(self, phone):
        actions = [play(60 + n, episode=f"https://example.com/a/ep{n}.mp3") for n in range(65)]
        assert len(phone.upload_episode_actions(USERNAME, actions)) == 3

        stored, cursor = phone.get_episode_actions(USERNAME, 0)
        assert len(stored) == 65
        assert phone.get_episode_actions(USERNAME, cursor)[0] == []

    def test_a_retried_sync_does_not_duplicate_history(self, phone):
        # AntennaPod re-uploads the whole batch when a sync fails, so this is the
        # ordinary case rather than an edge one.
        batch = [play(60 + n, episode=f"https://example.com/a/ep{n}.mp3") for n in range(10)]
        phone.upload_episode_actions(USERNAME, batch)
        phone.upload_episode_actions(USERNAME, batch)
        stored, _ = phone.get_episode_actions(USERNAME, 0)
        assert len(stored) == 10


class TestRefusals:
    def test_a_wrong_password_is_refused(self, server):
        client = _client(server)
        with pytest.raises(Unauthorised):
            client.login(USERNAME, "not-the-password")

    def test_repeated_failures_are_eventually_locked_out(self, server):
        client = _client(server)
        for _ in range(12):
            try:
                client.login(USERNAME, "not-the-password")
            except Unauthorised, SyncFailed:
                pass
        with pytest.raises(SyncFailed) as refusal:
            client.login(USERNAME, PASSWORD)
        # 429, not 401: the account is fine, the address is not.
        assert "429" in str(refusal.value)

    def test_one_account_cannot_read_anothers(self, phone):
        with pytest.raises(Unauthorised):
            phone.get_devices("someone-else")


def _client(server):
    from tests.fake_antennapod.client import FakeAntennaPod
    from tests.fake_antennapod.http_transport import HttpTransport

    return FakeAntennaPod(base_url=server.base_url, transport=HttpTransport())


class TestTheSecureCookieTrap:
    """The failure this project exists to fix, reproduced on purpose.

    A self-hoster serving over plain http on their LAN, with the default secure
    cookie left on, gets a login that succeeds and nothing afterwards that works.
    It looks exactly like a wrong password. The README says so; this proves the
    README is right, and that the escape hatch it points at actually works.
    """

    def test_a_secure_cookie_over_plain_http_breaks_everything_after_login(self, image):
        from tests.acceptance.containers import running

        container = running(
            GPODSYNC_SESSION_COOKIE_SECURE="true",
            GPODSYNC_BOOTSTRAP_USER=USERNAME,
            GPODSYNC_BOOTSTRAP_PASSWORD=PASSWORD,
        )
        try:
            container.wait_until_healthy()
            client = _client(container)

            # Login itself succeeds, which is the whole trap.
            client.login(USERNAME, PASSWORD)

            with pytest.raises(Unauthorised) as refusal:
                client.get_devices(USERNAME)

            # And the diagnosis is in the message rather than left to guesswork.
            assert "marked Secure" in str(refusal.value)
        finally:
            container.remove()

    def test_turning_it_off_is_the_documented_fix_and_works(self, phone):
        # The fixture's container runs with GPODSYNC_SESSION_COOKIE_SECURE=false,
        # which is what the README tells a LAN deployment to set.
        assert phone.get_devices(USERNAME) == []


class TestTheHealthCheckAnswersTheWayItIsDeployed:
    """The configuration a real operator writes, not the one the suite is easiest with.

    The health probe reaches /healthz/ over the loopback address. An operator who
    lists only their public hostname — the ordinary and correct thing to do — used
    to get a container that served traffic perfectly, reported itself unhealthy
    forever, and wrote a traceback into the log every thirty seconds. The rest of
    this suite could not see it, because its own containers list 127.0.0.1.
    """

    def test_a_container_listing_only_a_public_hostname_becomes_healthy(self, image):
        from tests.acceptance.containers import running

        container = running(GPODSYNC_ALLOWED_HOSTS="gpodder.example.com")
        try:
            container.wait_until_healthy()
            assert "DisallowedHost" not in container.logs()
        finally:
            container.remove()

    def test_startup_is_quiet(self, server):
        # Noise repeated on every boot trains operators to stop reading the
        # log, which is how a real startup problem slips past unread.
        logs = server.logs()
        assert "AXES: BEGIN" not in logs
        assert "lifespan" not in logs


def initial_sync(client, actions):
    """GET since=0 then upload — the exact order SyncService performs from cursor 0."""
    _, cursor = client.get_episode_actions(USERNAME, 0)
    if actions:
        client.upload_episode_actions(USERNAME, actions)
    return cursor


class TestAnInitialImportCannotRewriteHistory:
    """A device syncing from cursor 0 dumps its database stamped with the moment
    of the dump. The client applies whatever it downloads, so the server is the
    only place these fabricated stamps can be defused."""

    def test_a_stale_dump_stamped_now_does_not_rewind_real_progress(self, phone, server):
        # Real listening on the phone, then an old tablet enabling sync: its
        # dump carries an older position wearing a newer stamp — the attack.
        phone.upload_episode_actions(USERNAME, [play(900, at="2026-08-26T19:20:00")])

        tablet = device(server, "tablet")
        initial_sync(tablet, [play(500, at="2026-08-27T09:00:00")])

        actions, _ = phone.get_episode_actions(USERNAME, 0)
        assert [a["position"] for a in actions] == [900]

    def test_a_dump_fills_a_vacuum(self, phone, server):
        unknown = "https://example.com/a/only-on-the-tablet.mp3"
        tablet = device(server, "tablet")
        initial_sync(tablet, [play(700, episode=unknown, at="2026-08-27T09:00:00")])

        actions, _ = phone.get_episode_actions(USERNAME, 0)
        assert [a["episode"] for a in actions] == [unknown]
        assert [a["position"] for a in actions] == [700]
        # The demoted stamp crosses the wire in the exact shape the client
        # parses as epoch zero, so any future real action overrides it.
        assert actions[0]["timestamp"] == "1970-01-01T00:00:00"

    def test_an_interrupted_dump_repeats_in_a_new_session_without_duplicating(self, server):
        dump = [play(300, episode=f"https://example.com/a/{n}.mp3") for n in range(3)]

        tablet = device(server, "tablet")
        initial_sync(tablet, dump)
        # AntennaPod saves its cursor only after a complete upload, so a retry
        # re-dumps everything from since=0, re-stamped with a fresh wall time.
        retried = device(server, "tablet")
        restamped = [dict(action, timestamp="2026-08-27T09:05:00") for action in dump]
        initial_sync(retried, restamped)

        observer = device(server, "phone")
        actions, cursor = observer.get_episode_actions(USERNAME, 0)
        assert len(actions) == 3
        again, _ = observer.get_episode_actions(USERNAME, cursor)
        assert again == []

    def test_a_forced_full_sync_on_an_established_device_changes_nothing_served(
        self, phone, server
    ):
        phone.upload_episode_actions(USERNAME, [play(900, at="2026-08-26T19:20:00")])

        # Force full sync: the same device starts over from cursor 0 and dumps
        # its current state the way the exporter builds it — started == position,
        # total == duration, a fabricated now-stamp.
        started_over = device(server, "phone")
        export = dict(play(900, at="2026-08-27T09:00:00"), started=900, total=1800)
        initial_sync(started_over, [export])

        actions, _ = device(server, "tablet").get_episode_actions(USERNAME, 0)
        assert [a["position"] for a in actions] == [900]
        assert actions[0]["timestamp"] == "2026-08-26T19:20:00"

    def test_a_fresh_device_receives_one_action_per_episode(self, phone, server):
        phone.upload_episode_actions(USERNAME, [play(300, at="2026-08-26T19:10:00")])
        phone.upload_episode_actions(USERNAME, [play(600, at="2026-08-26T19:20:00")])
        phone.upload_episode_actions(USERNAME, [play(900, at="2026-08-26T19:30:00")])

        actions, _ = device(server, "tablet").get_episode_actions(USERNAME, 0)
        assert [a["position"] for a in actions] == [900]
