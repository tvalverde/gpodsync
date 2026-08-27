"""The cursor semantics, which are the part of storage that can silently lose data.

`since` is an opaque cursor the client echoes back, not a clock. Everything here
is about one question: after a client says "I last saw N", does it receive
exactly what it has not seen, once?
"""

from datetime import UTC, datetime

import pytest
from django.contrib.auth import get_user_model

from gpodsync.models import Device, EpisodeActionRecord, SubscriptionChange

pytestmark = [pytest.mark.component, pytest.mark.django_db]

FEED_A = "https://example.com/a.xml"
FEED_B = "https://example.com/b.xml"


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username="toni", password="x" * 16)


@pytest.fixture
def device(account):
    return Device.objects.create(user=account, device_id="phone", caption="Phone")


def change(account, device, url, action):
    return SubscriptionChange.objects.create(user=account, device=device, url=url, action=action)


def an_action_row(account, device, **overrides):
    fields = {
        "user": account,
        "device": device,
        "podcast": FEED_A,
        "episode": "https://example.com/ep1.mp3",
        "action": EpisodeActionRecord.PLAY,
        "happened_at": datetime(2026, 8, 26, 19, 10, tzinfo=UTC),
    } | overrides
    return EpisodeActionRecord(**fields)


def store(*rows):
    # The same path the view takes: the constraint does the deduplication, not
    # a check-then-insert that two concurrent uploads could race past.
    EpisodeActionRecord.objects.bulk_create(rows, ignore_conflicts=True)


class TestCurrentSubscriptions:
    def test_the_latest_word_on_a_url_wins(self, account, device):
        change(account, device, FEED_A, SubscriptionChange.ADD)
        change(account, device, FEED_A, SubscriptionChange.REMOVE)
        change(account, device, FEED_A, SubscriptionChange.ADD)
        assert SubscriptionChange.current_urls(user_id=account.id) == [FEED_A]

    def test_a_removed_feed_is_gone(self, account, device):
        change(account, device, FEED_A, SubscriptionChange.ADD)
        change(account, device, FEED_B, SubscriptionChange.ADD)
        change(account, device, FEED_A, SubscriptionChange.REMOVE)
        assert SubscriptionChange.current_urls(user_id=account.id) == [FEED_B]

    def test_a_device_with_no_history_has_nothing(self, account, device):
        assert SubscriptionChange.current_urls(user_id=account.id) == []


class TestChangesSince:
    def test_reports_only_what_came_after_the_cursor(self, account, device):
        first = change(account, device, FEED_A, SubscriptionChange.ADD)
        change(account, device, FEED_B, SubscriptionChange.ADD)
        added, removed, cursor = SubscriptionChange.changes_since(
            user_id=account.id, since=first.id
        )
        assert added == [FEED_B]
        assert removed == []
        assert cursor > first.id

    def test_a_feed_added_then_removed_appears_once_as_a_removal(self, account, device):
        # Reporting both would be truthful about the log and useless to a client,
        # which only wants to know what its list should look like now.
        change(account, device, FEED_A, SubscriptionChange.ADD)
        change(account, device, FEED_A, SubscriptionChange.REMOVE)
        added, removed, _ = SubscriptionChange.changes_since(user_id=account.id, since=0)
        assert added == []
        assert removed == [FEED_A]

    def test_nothing_new_returns_nothing(self, account, device):
        latest = change(account, device, FEED_A, SubscriptionChange.ADD)
        added, removed, cursor = SubscriptionChange.changes_since(
            user_id=account.id, since=latest.id
        )
        assert (added, removed) == ([], [])
        assert cursor == latest.id

    def test_the_cursor_never_moves_backwards(self, account, device):
        # The client stores whatever it is given and asks for everything after
        # it. A cursor that went backwards would replay history forever.
        _, _, cursor = SubscriptionChange.changes_since(user_id=account.id, since=9999)
        assert cursor == 9999

    def test_another_account_is_invisible(self, account, device):
        stranger = get_user_model().objects.create_user(username="other", password="y" * 16)
        their_device = Device.objects.create(user=stranger, device_id="phone")
        change(stranger, their_device, FEED_A, SubscriptionChange.ADD)
        added, removed, _ = SubscriptionChange.changes_since(user_id=account.id, since=0)
        assert (added, removed) == ([], [])


class TestEpisodeActionPaging:
    def record(self, account, device, episode):
        return EpisodeActionRecord.objects.create(
            user=account,
            device=device,
            podcast=FEED_A,
            episode=episode,
            action=EpisodeActionRecord.PLAY,
            happened_at=datetime(2026, 8, 26, 19, 10, tzinfo=UTC),
        )

    def test_returns_everything_when_it_fits(self, account, device):
        for n in range(3):
            self.record(account, device, f"https://example.com/{n}.mp3")
        rows, cursor = EpisodeActionRecord.since(user_id=account.id, since=0, limit=10)
        assert len(rows) == 3
        assert cursor == rows[-1].id

    def test_caps_the_page_and_points_at_the_last_row_included(self, account, device):
        # The escape hatch for an unbounded history: the client stores this
        # cursor and collects the rest on its next sync, which is ordinary
        # protocol behaviour rather than something it has to be taught.
        for n in range(10):
            self.record(account, device, f"https://example.com/{n}.mp3")
        rows, cursor = EpisodeActionRecord.since(user_id=account.id, since=0, limit=4)
        assert len(rows) == 4
        assert cursor == rows[-1].id

        rest, _ = EpisodeActionRecord.since(user_id=account.id, since=cursor, limit=100)
        assert len(rest) == 6

    def test_an_empty_history_holds_the_cursor_still(self, account, device):
        rows, cursor = EpisodeActionRecord.since(user_id=account.id, since=42, limit=10)
        assert rows == []
        assert cursor == 42

    def test_another_account_is_invisible(self, account, device):
        stranger = get_user_model().objects.create_user(username="other", password="y" * 16)
        their_device = Device.objects.create(user=stranger, device_id="phone")
        self.record(stranger, their_device, "https://example.com/theirs.mp3")
        rows, _ = EpisodeActionRecord.since(user_id=account.id, since=0, limit=10)
        assert rows == []


class TestDevicesShareOneList:
    """Which device reported a change is recorded; it does not partition anything.

    An earlier version of this file asserted the opposite, and a test in the
    acceptance suite was renamed to match it. Both were faithful descriptions of
    what the code did and both cemented a behaviour that made a second phone
    useless — which is the case this server exists to serve.
    """

    def test_a_feed_removed_on_one_device_is_removed_for_the_account(self, account, device):
        tablet = Device.objects.create(user=account, device_id="tablet")
        change(account, device, FEED_A, SubscriptionChange.ADD)
        change(account, tablet, FEED_A, SubscriptionChange.REMOVE)

        assert SubscriptionChange.current_urls(user_id=account.id) == []

    def test_a_feed_added_on_one_device_belongs_to_the_account(self, account, device):
        Device.objects.create(user=account, device_id="tablet")
        change(account, device, FEED_A, SubscriptionChange.ADD)

        assert SubscriptionChange.current_urls(user_id=account.id) == [FEED_A]

    def test_deleting_a_device_takes_its_reported_changes_with_it(self, account, device):
        # A consequence of the cascade rather than a decision: the log rows carry
        # the device, so removing the device removes its rows. Worth pinning,
        # because it means retiring a phone rewrites what the account looks like.
        tablet = Device.objects.create(user=account, device_id="tablet")
        change(account, device, FEED_A, SubscriptionChange.ADD)
        change(account, tablet, FEED_B, SubscriptionChange.ADD)

        tablet.delete()

        assert SubscriptionChange.current_urls(user_id=account.id) == [FEED_A]

    def test_deleting_a_device_leaves_episode_history_intact(self, account, device):
        # Episode actions belong to the account, not to the device that reported
        # them, so losing a phone must not lose where you were in a series.
        EpisodeActionRecord.objects.create(
            user=account,
            device=device,
            podcast=FEED_A,
            episode="https://example.com/ep1.mp3",
            action=EpisodeActionRecord.PLAY,
            happened_at=datetime(2026, 8, 26, 19, 10, tzinfo=UTC),
        )
        device.delete()

        rows, _ = EpisodeActionRecord.since(user_id=account.id, since=0, limit=10)
        assert len(rows) == 1
        assert rows[0].device is None


class TestContentIdempotency:
    """A re-report of identical content is the same report, whenever it claims
    to have happened. A device re-importing its database re-stamps everything
    with a fresh wall time; keying on the moment would duplicate the history
    on every retry."""

    def test_the_same_content_reported_at_two_moments_is_one_row(self, account, device):
        first = datetime(2026, 8, 26, 19, 10, tzinfo=UTC)
        store(an_action_row(account, device, position=120, happened_at=first))
        store(
            an_action_row(
                account,
                device,
                position=120,
                happened_at=datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
            )
        )
        rows = list(EpisodeActionRecord.objects.all())
        assert len(rows) == 1
        assert rows[0].happened_at == first

    def test_a_different_position_in_the_same_second_is_two_rows(self, account, device):
        store(an_action_row(account, device, position=120))
        store(an_action_row(account, device, position=450))
        assert EpisodeActionRecord.objects.count() == 2

    def test_a_different_guid_is_two_rows(self, account, device):
        store(an_action_row(account, device, guid="a"))
        store(an_action_row(account, device, guid="b"))
        assert EpisodeActionRecord.objects.count() == 2

    def test_a_bare_play_re_reported_later_is_one_row(self, account, device):
        # All offsets absent: the Coalesce arm of the key.
        store(an_action_row(account, device))
        store(an_action_row(account, device, happened_at=datetime(2026, 8, 27, 9, 0, tzinfo=UTC)))
        assert EpisodeActionRecord.objects.count() == 1


class TestWinnerServing:
    """A read serves the current winner of each (episode, action) group, never
    the raw log. The client applies any downloaded action without consulting
    its own state, so a superseded row that crosses the wire is a rewind."""

    SENTINEL = datetime(1970, 1, 1, tzinfo=UTC)
    REAL = datetime(2026, 8, 26, 19, 10, tzinfo=UTC)

    def served(self, account, since=0, limit=10):
        rows, cursor = EpisodeActionRecord.since(user_id=account.id, since=since, limit=limit)
        return rows, cursor

    @pytest.mark.parametrize("sentinel_first", [True, False])
    def test_a_real_timestamp_beats_the_sentinel(self, account, device, sentinel_first):
        weak = an_action_row(account, device, position=500, happened_at=self.SENTINEL)
        strong = an_action_row(account, device, position=900, happened_at=self.REAL)
        store(*((weak, strong) if sentinel_first else (strong, weak)))

        rows, _ = self.served(account)
        assert [row.position for row in rows] == [900]

    def test_the_sentinel_serves_when_it_is_the_only_claim(self, account, device):
        store(an_action_row(account, device, position=500, happened_at=self.SENTINEL))
        rows, _ = self.served(account)
        assert [row.position for row in rows] == [500]

    def test_equal_timestamps_serve_the_lowest_id(self, account, device):
        # The mirror of the client's strictly-after override: on a tie, the
        # first-processed action sticks there, so the first row sticks here.
        store(an_action_row(account, device, position=120))
        store(an_action_row(account, device, position=450))
        rows, _ = self.served(account)
        assert [row.position for row in rows] == [120]

    def test_a_second_sentinel_dump_does_not_displace_the_first(self, account, device):
        # Two equally-weak claims: a later stale import must not overwrite an
        # earlier one, or enabling sync on old devices becomes a race.
        store(an_action_row(account, device, position=500, happened_at=self.SENTINEL))
        store(an_action_row(account, device, position=100, happened_at=self.SENTINEL))
        rows, _ = self.served(account)
        assert [row.position for row in rows] == [500]

    def test_a_suppressed_tail_still_advances_the_cursor(self, account, device):
        # The winner is served, the stale row behind it is not — and the cursor
        # must clear the stale row anyway, or every future sync rescans it.
        store(an_action_row(account, device, position=900, happened_at=self.REAL))
        store(an_action_row(account, device, position=500, happened_at=self.SENTINEL))

        rows, cursor = self.served(account)
        assert [row.position for row in rows] == [900]
        newest = EpisodeActionRecord.cursor_for(user_id=account.id, floor=0)
        assert cursor == newest
        assert cursor > rows[-1].id

    def test_a_winner_below_the_cursor_suppresses_a_stale_row_above_it(self, account, device):
        # The superseding row may already have been delivered; the stale one
        # arriving later must still lose to it, so the winner subquery cannot
        # be restricted to the page's own window.
        store(an_action_row(account, device, position=900, happened_at=self.REAL))
        winner_id = EpisodeActionRecord.objects.get().id
        store(an_action_row(account, device, position=500, happened_at=self.SENTINEL))

        rows, cursor = self.served(account, since=winner_id)
        assert rows == []
        assert cursor > winner_id

    def test_pagination_walks_sparse_winners(self, account, device):
        for n in range(6):
            episode = f"https://example.com/{n}.mp3"
            store(
                an_action_row(
                    account, device, episode=episode, position=100, happened_at=self.SENTINEL
                )
            )
            store(
                an_action_row(account, device, episode=episode, position=200, happened_at=self.REAL)
            )

        first_page, cursor = self.served(account, limit=4)
        assert [row.position for row in first_page] == [200] * 4
        assert cursor == first_page[-1].id

        rest, final = self.served(account, since=cursor, limit=100)
        assert [row.position for row in rest] == [200] * 2
        served_episodes = {row.episode for row in first_page} | {row.episode for row in rest}
        assert len(served_episodes) == 6
        assert final == EpisodeActionRecord.cursor_for(user_id=account.id, floor=0)

    def test_each_action_type_is_its_own_group(self, account, device):
        store(
            an_action_row(
                account, device, action=EpisodeActionRecord.DOWNLOAD, happened_at=self.SENTINEL
            )
        )
        store(
            an_action_row(
                account,
                device,
                action=EpisodeActionRecord.PLAY,
                position=900,
                happened_at=self.REAL,
            )
        )
        rows, _ = self.served(account)
        assert sorted(row.action for row in rows) == ["download", "play"]

    def test_another_accounts_row_does_not_suppress(self, account, device):
        stranger = get_user_model().objects.create_user(username="other", password="y" * 16)
        their_device = Device.objects.create(user=stranger, device_id="phone")
        store(an_action_row(stranger, their_device, position=900, happened_at=self.REAL))
        store(an_action_row(account, device, position=500, happened_at=self.SENTINEL))

        rows, _ = self.served(account)
        assert [row.position for row in rows] == [500]
