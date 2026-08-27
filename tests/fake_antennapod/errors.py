"""How the real client fails, reproduced so tests fail the same way."""


class SyncFailed(Exception):
    """What AntennaPod surfaces to the user for any failure at all.

    The real client makes no distinction between a rejected password, a malformed
    response and a network fault: `SyncService` catches everything, shows "sync
    failed" and retries later. Tests should assert on the subclasses; this exists
    so that a test can also assert the user-visible outcome.
    """


class Unauthorised(SyncFailed):
    """A 401. Reported to the user as wrong credentials, whatever caused it."""


class MalformedResponse(SyncFailed):
    """A response the client's JSON parsing cannot survive.

    Raised where the Java would throw: `getJSONArray` and `getLong` on an absent
    key throw rather than returning a default, so an omitted `update_urls` is not
    a degraded response — it is an exception on the phone.
    """
