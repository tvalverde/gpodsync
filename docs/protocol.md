# The wire contract

What AntennaPod actually sends and what it will accept back. Taken from the
client's source on the `develop` branch — `GpodnetService.java`,
`EpisodeAction.java`, `ResponseMapper.java`, `AntennapodHttpClient.java` and
`SyncService.java` — rather than from the gpodder.net documentation, which is
correct but does not describe where the client is stricter than the specification.

Where the two disagree, the client wins. It is the thing that has to work.

## Authentication is a session cookie, and only a session cookie

This is the single most important fact here, and the reason several otherwise
reasonable servers fail.

`GpodnetService.login()` attaches `Authorization: Basic` to
`POST /api/2/auth/{username}/login.json`. **No other request carries it.**
`executeRequest()` builds every subsequent call without credentials, and the
client's `BasicAuthorizationInterceptor` re-attaches them only to requests tagged
as feed downloads — never to gpodder API calls.

So the cookie set at login authenticates everything that follows, and two of its
attributes decide whether the client will ever send it back:

- **`Domain`** — the cookie jar is a `JavaNetCookieJar` over a `CookieManager` set
  to `CookiePolicy.ACCEPT_ORIGINAL_SERVER`. A cookie whose `Domain` does not match
  the origin host exactly is discarded without a word. Emit a host-only cookie:
  no `Domain` attribute at all.
- **`Secure`** — Java will not return a `Secure` cookie over `http://`. On an
  HTTPS deployment this is what you want; on a plain-HTTP LAN deployment it makes
  the server unusable.

Both failures present identically: login returns 200, every later request returns
401, and the user is told their password is wrong.

`SameSite` and `HttpOnly` are safe to set. `java.net.HttpCookie` does parse
`HttpOnly` — it has an `isHttpOnly()` — but the jar does not act on either when
deciding what to send back, and there is no browser in this picture anyway.

The client calls `login()` at the **start of every sync**, not once per session,
so frequent successful logins are normal traffic. Rate limiting should count
failures, not requests.

## Redirects break authentication

A redirect breaks login, but by two different mechanisms depending on where it
goes — and the popular one-line explanation is wrong, so it is worth being exact.

`RetryAndFollowUpInterceptor` strips `Authorization` only when the redirect
target cannot reuse the connection, that is when scheme, host or port change. So:

* **Same origin** — `APPEND_SLASH` adding a trailing slash — keeps the header.
  What breaks it instead is that OkHttp rewrites a 301 or 302 into a **GET**, so
  the login endpoint is asked for a method it does not answer.
* **Cross scheme** — `SECURE_SSL_REDIRECT` sending http to https — really does
  drop the credentials.

Either way the user reads "wrong username or password". Both settings must be
off, and no proxy in front may redirect these paths either. The operational rule
is simply that nothing under `/api/` may return 3xx.

## The endpoints

All paths are relative to the configured host, optionally under a subfolder — the
client's `HostnameParser` accepts `https://host:port/subfolder` and assumes
`https` when no scheme is given.

| Method | Path | Request | Response |
|---|---|---|---|
| POST | `/api/2/auth/{user}/login.json` | empty body, `Content-Type: plain/text; charset=utf-8`, Basic auth | status only; must set the session cookie |
| GET | `/api/2/devices/{user}.json` | — | array of device objects |
| POST | `/api/2/devices/{user}/{device}.json` | `{"caption": …, "type": …}` | status only |
| GET | `/api/2/subscriptions/{user}/{device}.json?since={int}` | — | `{"add": [], "remove": [], "timestamp": int}` |
| POST | `/api/2/subscriptions/{user}/{device}.json` | `{"add": [], "remove": []}` | `{"timestamp": int, "update_urls": []}` |
| GET | `/api/2/episodes/{user}.json?since={int}` | — | `{"actions": [], "timestamp": int}` |
| POST | `/api/2/episodes/{user}.json` | array of actions, **30 per request** | `{"timestamp": int, "update_urls": []}` |

Note the login `Content-Type`. It is not a typo in this document — the client
really does send `plain/text`, which is not a real media type, and a framework
that negotiates content types will reject the request before your code sees it.

## Fields that crash the client when missing

The response mappers use `getJSONArray` and `getLong`, which throw rather than
return a default. A field omitted here is not a degraded response; it is an
exception on the phone.

- `add`, `remove` and `timestamp` on subscription changes.
- `actions` and `timestamp` on episode actions.
- `timestamp` and `update_urls` on both POST responses. **`"update_urls": []` is
  mandatory**, not an optional extra.
- `id`, `caption`, `type` and `subscriptions` on each device object, with
  `subscriptions` an integer.

`update_urls` is an array of `[original, sanitised]` pairs. The client parses it
into a map and then — as of `develop` — does nothing with it: grep the tree and
the only references are the constructor and `toString()`. So the field is
mandatory but inert, and returning `[]` unless sanitisation genuinely changed a
URL is a matter of not lying to a client that may one day start listening, rather
than of avoiding damage today.

The client also strips `%3A` back to `:` in returned subscription URLs, working
around gpodder.net escaping colons unnecessarily. Don't escape them.

## Episode actions

```json
{
  "podcast": "https://example.com/feed.xml",
  "episode": "https://example.com/ep1.mp3",
  "guid": "optional",
  "action": "play",
  "timestamp": "2026-08-26T19:10:00",
  "started": 0, "position": 120, "total": 500
}
```

- `action` is one of `new`, `download`, `play`, `delete`. The client reads it with
  `Action.valueOf(value.toUpperCase(Locale.US))`, so capitalisation does not
  matter on receipt — an unrecognised *name* is what gets dropped. It sends
  lowercase, and emitting lowercase is the safe choice.
- `timestamp` is `yyyy-MM-dd'T'HH:mm:ss` in UTC — **no `Z` suffix, no offset, no
  fractional seconds**. The client parses with a fixed `SimpleDateFormat`, and the
  failure mode is worse than rejection: `SimpleDateFormat.parse` ignores trailing
  text, so `…:00Z` and `…:00.123` are accepted with the suffix discarded, and
  `…:00+02:00` is accepted and read as UTC. That last one is a two-hour error
  applied silently to somebody's listening history. Emit the exact format.
- `podcast`, `episode` and `action` are mandatory; an action missing any of them is
  discarded.
- For `play`, the position triple is accepted only when
  `started >= 0 && position > 0 && total > 0`. A `play` action with `position: 0`
  is read as a bare `play` with no position — which is why a paused-at-the-start
  episode never seems to sync.
- `device` is added by the client to every uploaded action.

## The initial import, and why the server must defend against it

When the client's stored cursor is 0 — a fresh setup, or "force full
synchronisation" — `SyncService.syncEpisodeActions` first reads
`GET /api/2/episodes/{user}.json?since=0` and then uploads a full export of its
database: one `play` per played episode, `started = position`,
`total = duration`, and **every action stamped with the same
`.currentTimestamp()`** — the moment of the dump, not of the listening. The
cursor is saved only after the whole upload succeeds, so an interrupted dump
repeats entirely on the next sync, re-stamped with a fresh wall time, again from
`since=0`.

The client does not defend itself against what this produces.
`EpisodeActionFilter.getRemoteActionsOverridingLocalActions` compares a download
only against other actions in the same download and against the local *unsent*
queue — never against already-applied state. Any downloaded `play` is applied,
and its override rule requires timestamps strictly after one another; on a tie,
the first action processed sticks. So a device with stale history enabling sync
uploads old positions wearing brand-new stamps, and every other device on the
account applies them, rewinding real progress. Nobody notices until they reach
for an episode they had finished.

gpodsync therefore treats an import as a weak claim, in three parts that only
work together:

- **Detection**: the session that reads episode actions with `since=0` is about
  to dump, so it is flagged; everything it uploads afterwards is import data.
  The flag lives in the session because the client logs in per sync, reads
  before it uploads, and re-enters through `since=0` when it retries. The first
  read from a real cursor clears it. Only a cookie-backed session is flagged:
  the client always holds the cookie after login, so a cookie-less Basic read
  cannot be mid-dump, and flagging one would only mint orphan session rows.
- **Demotion**: imported actions are stored — and served — with the timestamp
  `1970-01-01T00:00:00`, the claim that loses every strictly-after comparison
  the client makes. It fills a vacuum on a device that knows nothing and
  overrides nothing anywhere else.
- **Winner serving**: `GET` returns only the current winner of each
  (podcast, episode, action) group — greatest timestamp, ties to the earliest
  row — because the client applies whatever crosses the wire. A superseded row
  served anyway *is* the rewind, however honest the log is about holding it.

Idempotency is keyed on the content of an action rather than on its reported
moment for the same reason: a repeated dump re-stamps identical content, and a
key carrying the moment would duplicate the whole history on every retry.

Verified against AntennaPod 3.11.4, 3.12.0 and `develop` — identical in every
part described here.

## Subscriptions are per device on the wire, and shared here

The endpoints are addressed per device, and read literally that is what they
mean: each device has its own list. gpodder.net reconciles devices through a
sync-group endpoint at `/api/2/sync-devices/{user}.json`, which **AntennaPod
never calls**.

Follow the letter of it on a self-hosted server and the result is a second phone
that syncs successfully, receives an empty list, and stays empty forever — while
every status code and every mandatory field is correct.

gpodsync therefore treats subscriptions as belonging to the account. Reads are
account-wide, so a device asking from zero receives the whole current list, which
is what a fresh installation needs. Which device reported a change is still
recorded, because it is worth knowing; it just does not partition what anybody
can see. Episode actions were already account-wide, for the same reason.

## `since`

An opaque `long`. The client stores whatever `timestamp` a response carried and
sends it back unchanged on the next request. It is not required to be a Unix time,
and using one invites collisions when two changes land in the same second.
gpodsync uses a monotonic per-user counter.

Because the client only ever asks for "everything after this cursor", a server may
return a partial page and a cursor pointing at the last item it included; the rest
arrives on the next sync. That is the escape hatch for an unbounded history.
