"""The seven endpoints.

Two rules run through all of them and are worth stating once rather than at every
return statement.

**Exactly 200.** `GpodnetService.checkStatusCode` throws on any other code, so a
201 Created for a POST that creates something, or a 204 for one that returns
nothing, is a failed sync on the phone. The natural REST answer is the wrong one
here.

**Every mandatory field, every time.** The client reads `update_urls` with
`getJSONArray` and `timestamp` with `getLong`, both of which throw on an absent
key. An omitted field is not a smaller response; it is an exception on a device.
"""

import logging
from typing import Any, cast

from django.contrib.auth import login as start_session
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone

from gpodsync.api.base import (
    GpodderApiView,
    authenticate_with_basic,
    authenticated_as,
    bad_request,
    refuse,
)
from gpodsync.domain.episode_actions import (
    IMPORT_SENTINEL,
    Action,
    EpisodeAction,
    InvalidAction,
    parse_action,
    serialise_action,
)
from gpodsync.domain.feeds import InvalidFeedUrl, sanitise_feed_urls
from gpodsync.domain.identifiers import InvalidDeviceId, validate_device_id
from gpodsync.models import Device, EpisodeActionRecord, SubscriptionChange

# Bounds far above anything a client sends — a real batch is thirty actions — and
# far below anything that would exhaust the process. They exist so that an
# unbounded body cannot become an unbounded amount of work.
MAX_ACTIONS_PER_REQUEST = 1_000
MAX_URLS_PER_REQUEST = 5_000
MAX_ACTIONS_PER_PAGE = 1_000

logger = logging.getLogger("gpodsync.sync")


class LoginView(GpodderApiView):
    """`POST /api/2/auth/{username}/login.json`.

    The only endpoint that reads credentials. Everything after this one is
    carried by the cookie it sets, which is why that cookie's attributes deserve
    more attention than the rest of this file put together.
    """

    requires_authentication = False

    def post(self, request: HttpRequest, username: str, *args: Any, **kwargs: Any) -> HttpResponse:
        user = authenticate_with_basic(request)
        if user is None:
            # Covers an absent header, an unparseable one, an unknown account and
            # a wrong password. The client cannot tell them apart, deliberately;
            # the log distinguishes only as far as the backend told us.
            return refuse("credentials_rejected", path_username=username)
        if not authenticated_as(user, username):
            return refuse("username_mismatch", path_username=username)

        # Rotates the session key, closing session fixation: an identifier chosen
        # before login is not the one the account ends up holding.
        # authenticate() returns the base type; login()'s stubs want the
        # concrete user model, and it is the same object either way.
        start_session(request, cast(Any, user))
        return HttpResponse(status=200)


class DeviceListView(GpodderApiView):
    """`GET /api/2/devices/{username}.json`."""

    def get(self, request: HttpRequest, username: str, *args: Any, **kwargs: Any) -> HttpResponse:
        devices = Device.objects.filter(user_id=self.account_id).order_by("device_id")
        payload = [
            {
                "id": device.device_id,
                "caption": device.caption,
                "type": device.kind,
                # An integer, not a string. The client reads it with getInt.
                # The account's list, which every device now shares.
                "subscriptions": len(SubscriptionChange.current_urls(user_id=self.account_id)),
            }
            for device in devices
        ]
        return JsonResponse(payload, safe=False)


class DeviceConfigView(GpodderApiView):
    """`POST /api/2/devices/{username}/{device}.json`."""

    def post(
        self, request: HttpRequest, username: str, device: str, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            device_id = validate_device_id(device)
        except InvalidDeviceId as error:
            return bad_request(str(error))

        try:
            body = self.json_body(request)
        except ValueError as error:
            return bad_request(str(error))
        if not isinstance(body, dict):
            return bad_request("expected a JSON object")

        record, _ = Device.objects.get_or_create(user_id=self.account_id, device_id=device_id)
        record.caption = str(body.get("caption") or record.caption or device_id)[:255]
        record.kind = str(body.get("type") or record.kind or "other")[:32]
        record.save(update_fields=["caption", "kind"])

        return HttpResponse(status=200)


class SubscriptionsView(GpodderApiView):
    """`GET`/`POST /api/2/subscriptions/{username}/{device}.json`."""

    def get(
        self, request: HttpRequest, username: str, device: str, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        record = self._device(request, device)
        if isinstance(record, HttpResponse):
            return record

        since = self.since(request)
        if since is None:
            return bad_request("since must be a whole number")

        added, removed, cursor = SubscriptionChange.changes_since(
            user_id=self.account_id, since=since
        )
        logger.info(
            "subscriptions since %s: %s added, %s removed",
            since,
            len(added),
            len(removed),
            extra={
                "event": "subscriptions_read",
                "device": record.device_id,
                "since": since,
                "added": len(added),
                "removed": len(removed),
                "cursor": cursor,
            },
        )
        return JsonResponse({"add": added, "remove": removed, "timestamp": cursor})

    def post(
        self, request: HttpRequest, username: str, device: str, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        record = self._device(request, device)
        if isinstance(record, HttpResponse):
            return record

        try:
            body = self.json_body(request)
        except ValueError as error:
            return bad_request(str(error))
        if not isinstance(body, dict):
            return bad_request("expected a JSON object")

        raw_add = body.get("add") or []
        raw_remove = body.get("remove") or []
        if not isinstance(raw_add, list) or not isinstance(raw_remove, list):
            return bad_request("add and remove must be arrays")
        if len(raw_add) + len(raw_remove) > MAX_URLS_PER_REQUEST:
            return bad_request(f"at most {MAX_URLS_PER_REQUEST} URLs per request")

        try:
            added = sanitise_feed_urls(raw_add)
            removed = sanitise_feed_urls(raw_remove)
        except (InvalidFeedUrl, TypeError, AttributeError) as error:
            return bad_request(str(error))

        with transaction.atomic():
            # Record only what actually changes. Re-subscribing to something the
            # device already has is not a change, and logging it would advance
            # the cursor and announce a subscription the other devices already
            # know about.
            # Applied in order, updating the picture as it goes. Comparing both
            # halves against one snapshot taken at the start meant a batch
            # containing the same URL in `add` and in `remove` kept the add and
            # dropped the remove — so the outcome depended on the previous state
            # rather than on what was sent.
            current = set(SubscriptionChange.current_urls(user_id=self.account_id))
            changes = []
            for url in dict.fromkeys(added.urls):
                if url not in current:
                    changes.append(
                        SubscriptionChange(
                            user_id=self.account_id,
                            device=record,
                            url=url,
                            action=SubscriptionChange.ADD,
                        )
                    )
                    current.add(url)
            for url in dict.fromkeys(removed.urls):
                if url in current:
                    changes.append(
                        SubscriptionChange(
                            user_id=self.account_id,
                            device=record,
                            url=url,
                            action=SubscriptionChange.REMOVE,
                        )
                    )
                    current.discard(url)
            SubscriptionChange.objects.bulk_create(changes)
            cursor = SubscriptionChange.cursor_for(user_id=self.account_id, floor=0)

        logger.info(
            "subscriptions upload: %s of %s were real changes",
            len(changes),
            len(raw_add) + len(raw_remove),
            extra={
                "event": "subscriptions_written",
                "device": record.device_id,
                "offered": len(raw_add) + len(raw_remove),
                "recorded": len(changes),
                "cursor": cursor,
            },
        )

        return JsonResponse(
            {
                "timestamp": cursor,
                # Mandatory even when empty, and empty unless sanitisation really
                # changed a URL.
                "update_urls": [list(pair) for pair in added.update_pairs + removed.update_pairs],
            }
        )

    def _device(self, request: HttpRequest, device: str) -> Device | HttpResponse:
        try:
            device_id = validate_device_id(device)
        except InvalidDeviceId as error:
            return bad_request(str(error))
        # Created on first mention. The client configures a device and syncs it in
        # either order, and refusing the second would be a sync that never starts.
        record, _ = Device.objects.get_or_create(user_id=self.account_id, device_id=device_id)
        return record


class EpisodeActionsView(GpodderApiView):
    """`GET`/`POST /api/2/episodes/{username}.json`."""

    def get(self, request: HttpRequest, username: str, *args: Any, **kwargs: Any) -> HttpResponse:
        since = self.since(request)
        if since is None:
            return bad_request("since must be a whole number")

        if since == 0:
            # AntennaPod reads from zero immediately before dumping its whole
            # database with fabricated wall timestamps; this flag is how the
            # POSTs that follow in the same session know to treat the dump as
            # weak claims. An absent parameter parses as zero and is treated as
            # import-shaped too — the client always sends it.
            #
            # Cookie-backed sessions only: the client always holds the cookie
            # after login, so a cookie-less Basic reader cannot be mid-dump,
            # and flagging one would just mint an orphan session row per read.
            if request.session.session_key is not None:
                request.session["initial_import"] = True
        else:
            request.session.pop("initial_import", None)

        rows, cursor = EpisodeActionRecord.since(
            user_id=self.account_id, since=since, limit=MAX_ACTIONS_PER_PAGE
        )
        logger.info(
            "episode actions since %s: %s returned",
            since,
            len(rows),
            extra={
                "event": "episode_actions_read",
                "since": since,
                "returned": len(rows),
                "cursor": cursor,
                "capped": len(rows) == MAX_ACTIONS_PER_PAGE,
                "initial_import": since == 0,
            },
        )
        return JsonResponse({"actions": [_to_wire(row) for row in rows], "timestamp": cursor})

    def post(self, request: HttpRequest, username: str, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            body = self.json_body(request)
        except ValueError as error:
            return bad_request(str(error))
        if not isinstance(body, list):
            return bad_request("expected a JSON array of actions")
        if len(body) > MAX_ACTIONS_PER_REQUEST:
            return bad_request(f"at most {MAX_ACTIONS_PER_REQUEST} actions per request")

        if not all(isinstance(entry, dict) for entry in body):
            # Skipping them silently was the old behaviour, which is out of
            # keeping with how strictly everything else here is read.
            return bad_request("every action must be a JSON object")

        try:
            actions = [parse_action(entry) for entry in body]
        except InvalidAction as error:
            return bad_request(str(error))

        # Read, never cleared here: one dump spans many 30-action batches. A
        # dump's timestamps are the moment of the dump, not of the listening,
        # and stored verbatim they would outrank every other device's real
        # history on the next download. The sentinel is the claim that loses
        # every comparison the client makes; the content-keyed constraint and
        # the lowest-id tie-break make repeated dumps collapse instead of
        # displacing one another.
        importing = bool(request.session.get("initial_import"))

        devices: dict[str, Device] = {}
        records = []
        for action in actions:
            device = None
            if action.device:
                try:
                    device_id = validate_device_id(action.device)
                except InvalidDeviceId as error:
                    return bad_request(str(error))
                if device_id not in devices:
                    devices[device_id], _ = Device.objects.get_or_create(
                        user_id=self.account_id, device_id=device_id
                    )
                device = devices[device_id]

            records.append(
                EpisodeActionRecord(
                    user_id=self.account_id,
                    device=device,
                    podcast=action.podcast,
                    episode=action.episode,
                    guid=action.guid or "",
                    action=action.action.value,
                    # A client that omits the timestamp is saying "now", and now
                    # is when we heard about it.
                    happened_at=(
                        IMPORT_SENTINEL if importing else (action.timestamp or timezone.now())
                    ),
                    started=action.started,
                    position=action.position,
                    total=action.total,
                )
            )

        with transaction.atomic():
            # ignore_conflicts leans on the uniqueness constraint: a re-uploaded
            # batch inserts what is new and silently drops what was already
            # reported, which is what a retry should mean.
            before = EpisodeActionRecord.objects.filter(user_id=self.account_id).count()
            EpisodeActionRecord.objects.bulk_create(records, ignore_conflicts=True)
            after = EpisodeActionRecord.objects.filter(user_id=self.account_id).count()
            cursor = EpisodeActionRecord.cursor_for(user_id=self.account_id, floor=0)

        # How many of an upload were already known is the number that explains a
        # retry loop, and it is invisible from the response. A repeated dump now
        # logs stored: 0 — identical content collapses whatever its fresh
        # stamps claim.
        logger.info(
            "episode actions: %s offered, %s new",
            len(records),
            after - before,
            extra={
                "event": "episode_actions_written",
                "offered": len(records),
                "stored": after - before,
                "cursor": cursor,
                "imported": importing,
                "demoted": len(records) if importing else 0,
            },
        )

        # Episode URLs are stored verbatim and never rewritten, so this is always
        # empty — and always present.
        return JsonResponse({"timestamp": cursor, "update_urls": []})


def _to_wire(row: EpisodeActionRecord) -> dict[str, Any]:
    device = row.device
    return serialise_action(
        EpisodeAction(
            podcast=row.podcast,
            episode=row.episode,
            action=Action(row.action),
            timestamp=row.happened_at,
            guid=row.guid or None,
            started=row.started,
            position=row.position,
            total=row.total,
            device=device.device_id if device is not None else None,
        )
    )
