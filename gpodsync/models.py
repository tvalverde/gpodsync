"""Storage.

The central decision here is how `since` works. AntennaPod treats the value as
an opaque long: it stores whatever `timestamp` a response carried and hands it
back next time. It is a cursor, not a clock, and using epoch seconds — as
gpodder.net does — means two changes in the same second are indistinguishable and
one of them is lost.

So the cursor is a row id from an append-only log. That gives strict ordering for
free, makes "everything after this point" a primary-key range scan, and gives
subscription history as a by-product rather than as a second table to keep in
step.

It relies on ids becoming visible in the order they are assigned, which holds for
a single writer and is why one uvicorn worker is the supported configuration.
With concurrent writers a transaction could commit an id lower than one already
handed out, and the change under it would never be delivered.

One known limitation, inherited from the protocol rather than chosen. Episode
actions are account-wide, and the cursor returned by an upload is the account's
newest id — so if another device wrote between this device's last read and its
next upload, that write falls below the cursor this device now holds and it will
never ask for it. gpodder.net has the same gap with epoch timestamps, and the
server cannot close it because it does not know a device's read position at the
moment it writes. Subscriptions are unaffected: their log is per device, and only
that device writes to it.
"""

from typing import Self

from django.conf import settings
from django.db import models
from django.db.models import Exists, F, Max, OuterRef, Q, Value
from django.db.models.functions import Coalesce


class AppendOnlyLog(models.Model):
    """Shared shape of the two logs whose row ids are cursors."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)s_set"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True

    @classmethod
    def cursor_for(cls, *, user_id: int, floor: int) -> int:
        """The newest id this account has, never below the caller's own cursor.

        Account-wide rather than per device, deliberately. A device's cursor can
        therefore advance because another device did something, which is coarser
        than necessary but never loses a change. Narrowing it per device would
        break that monotonicity, so it is not the tightening it looks like.

        The floor is what stops a client from replaying history. It sends back
        whatever it last received; if a response ever carried a smaller value —
        because the account has no rows yet, or because the client is ahead of
        what we can see — it would ask for the same range forever.
        """
        # _default_manager rather than objects: this runs on an abstract base,
        # which has no manager of its own.
        newest = cls._default_manager.filter(user_id=user_id).aggregate(newest=Max("id"))["newest"]
        return max(newest or 0, floor)


class Device(models.Model):
    """A client installation, named by the client itself."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="devices"
    )
    device_id = models.CharField(max_length=64)
    caption = models.CharField(max_length=255, blank=True)
    # `type` on the wire; not a field name worth shadowing a builtin for.
    kind = models.CharField(max_length=32, default="other")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "device_id"], name="unique_device_per_user")
        ]

    def __str__(self) -> str:
        return f"{self.device_id} ({self.user_id})"


class SubscriptionChange(AppendOnlyLog):
    """Append-only. One row per subscribe or unsubscribe, ever."""

    ADD = "add"
    REMOVE = "remove"
    ACTIONS = {ADD: "subscribed", REMOVE: "unsubscribed"}

    device = models.ForeignKey(
        Device, on_delete=models.CASCADE, related_name="subscription_changes"
    )
    url = models.TextField()
    action = models.CharField(max_length=6, choices=ACTIONS)

    class Meta(AppendOnlyLog.Meta):
        abstract = False
        indexes = [models.Index(fields=["user", "device", "id"])]

    @classmethod
    def current_urls(cls, *, user_id: int) -> list[str]:
        """The account's subscriptions right now: the latest word on each URL.

        Account-wide, not per device. The protocol gives every device its own
        list, and gpodder.net reconciles them through a sync-group endpoint that
        AntennaPod never calls — so read literally, a second phone syncing for
        the first time receives an empty list and stays empty forever, which
        defeats the entire point of running a sync server.

        One shared list per account is what a self-hosted single-account server
        is for. Which device reported a change is still recorded, because it is
        worth knowing; it just does not partition what anybody can see.
        """
        latest = (
            cls.objects.filter(user_id=user_id)
            .values("url")
            .annotate(newest=Max("id"))
            .values("newest")
        )
        return list(
            cls.objects.filter(id__in=latest, action=cls.ADD)
            .order_by("url")
            .values_list("url", flat=True)
        )

    @classmethod
    def changes_since(cls, *, user_id: int, since: int) -> tuple[list[str], list[str], int]:
        """What changed after `since`, collapsed to one verdict per URL.

        Account-wide, for the reason in `current_urls`. A device asking from zero
        therefore receives the whole current list, which is exactly what a fresh
        installation needs; a device asking from its own cursor receives what has
        happened anywhere since.

        A URL added and then removed inside the same window appears once, as a
        removal. Reporting both would be truthful about the log and useless to a
        client, which only wants to know what its list should look like now.

        A device does see its own changes echoed back. That is deliberate: it is
        idempotent for the client, and excluding them would leave a reinstalled
        phone unable to recover the subscriptions it had reported itself.
        """
        window = cls.objects.filter(user_id=user_id, id__gt=since)
        latest_per_url = window.values("url").annotate(newest=Max("id")).values("newest")
        rows = cls.objects.filter(id__in=latest_per_url).order_by("url")

        added = [row.url for row in rows if row.action == cls.ADD]
        removed = [row.url for row in rows if row.action == cls.REMOVE]
        return added, removed, cls.cursor_for(user_id=user_id, floor=since)


class EpisodeActionRecord(AppendOnlyLog):
    """One reported interaction with one episode. Also append-only."""

    NEW = "new"
    DOWNLOAD = "download"
    PLAY = "play"
    DELETE = "delete"
    ACTIONS = {NEW: "new", DOWNLOAD: "download", PLAY: "play", DELETE: "delete"}

    device = models.ForeignKey(
        Device, on_delete=models.SET_NULL, null=True, blank=True, related_name="episode_actions"
    )
    podcast = models.TextField()
    episode = models.TextField()
    guid = models.CharField(max_length=255, blank=True, default="")
    action = models.CharField(max_length=8, choices=ACTIONS)
    # When the client says it happened, which is not when we heard about it: a
    # phone syncs long after the fact and both orderings matter.
    happened_at = models.DateTimeField()
    started = models.IntegerField(null=True, blank=True)
    position = models.IntegerField(null=True, blank=True)
    total = models.IntegerField(null=True, blank=True)

    class Meta(AppendOnlyLog.Meta):
        abstract = False
        indexes = [
            models.Index(fields=["user", "id"]),
            # The winner subquery in since() correlates on exactly this prefix.
            # The unique index below would likely serve, but leaning on SQLite
            # using the plain-column prefix of an expression index is
            # undocumented behaviour; this one is cheap and self-documenting.
            models.Index(
                fields=["user", "podcast", "episode", "action", "happened_at"],
                name="gpodsync_ep_winner_idx",
            ),
        ]
        constraints = [
            # A retried sync re-uploads the whole batch, and AntennaPod retries
            # on any failure. Without this, every retry doubles somebody's
            # history and redelivers the same moment to their other devices.
            # Enforced in the database rather than checked first, so two
            # concurrent uploads cannot both find nothing and both insert.
            #
            # Keyed on *what* was reported, never on *when* it was reported:
            # a device re-importing its database re-stamps identical content
            # with a fresh wall time (docs/protocol.md, the initial import), so
            # a key carrying the moment turns every re-import into a full
            # duplicate of the history. The first report sticks, keeping its
            # happened_at.
            #
            # The playback offsets belong in the key. Without them, a second
            # `play` in the same second with a different position — a pause, a
            # seek, two devices reporting the same instant — was indistinguishable
            # from a retry and was silently discarded.
            #
            # Coalesced rather than listed as plain nullable fields: SQLite treats
            # NULLs in a unique index as distinct from each other, so a bare
            # `position` column would stop deduplicating retries of a `play` that
            # carries no position at all.
            models.UniqueConstraint(
                F("user"),
                F("podcast"),
                F("episode"),
                F("action"),
                F("guid"),
                Coalesce("started", Value(-1)),
                Coalesce("position", Value(-1)),
                Coalesce("total", Value(-1)),
                name="one_row_per_reported_content",
            )
        ]

    @classmethod
    def since(cls, *, user_id: int, since: int, limit: int) -> tuple[list[Self], int]:
        """A bounded page of current winners, and the cursor to resume from.

        Bounded because a history of years would otherwise produce a response of
        no fixed size. Returning the cursor of the last row included is enough:
        the client stores it and asks for the rest on its next sync, which is
        ordinary protocol behaviour rather than a special case it has to know
        about.

        Only the current winner of each (podcast, episode, action) group is
        served — winner meaning the greatest happened_at, ties to the lowest id,
        the mirror of the client's strictly-after override rule. The client
        applies any downloaded action without consulting its own state, so a
        superseded row crossing the wire is a rewind on somebody's phone. The
        supersession check deliberately spans the whole log, not the page's
        window: the superseding row may sit below the cursor, already delivered.

        Suppressed rows still advance the cursor. When the scan is exhausted the
        cursor comes from cursor_for rather than the last row seen, so a
        suppressed tail is never rescanned on every sync forever.
        """
        superseded = cls.objects.filter(
            user_id=OuterRef("user_id"),
            podcast=OuterRef("podcast"),
            episode=OuterRef("episode"),
            action=OuterRef("action"),
        ).filter(
            Q(happened_at__gt=OuterRef("happened_at"))
            | Q(happened_at=OuterRef("happened_at"), id__lt=OuterRef("id"))
        )
        # select_related: _to_wire reads row.device.device_id, which without this
        # is one query per row — a thousand for a full page.
        rows = list(
            cls.objects.filter(user_id=user_id, id__gt=since)
            .filter(~Exists(superseded))
            .select_related("device")
            .order_by("id")[: limit + 1]
        )
        if len(rows) > limit:
            page = rows[:limit]
            return page, page[-1].id
        return rows, cls.cursor_for(user_id=user_id, floor=since)
