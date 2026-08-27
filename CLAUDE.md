# gpodsync — working notes

A gpodder.net-compatible sync server whose single purpose is to work correctly
with AntennaPod. `docs/protocol.md` holds the wire contract, verified against the
client's source rather than its documentation; read it before touching anything
under `gpodsync/api/`.

## Invariants

These are load-bearing. Changing one is a deliberate decision, not a refactor.

**The server never fetches a feed URL.** Subscription URLs are attacker-supplied
strings that we validate, store and echo back — nothing more. This is true by
construction today, and writing it down is the point: "let's validate the feed by
downloading it" is a reasonable-sounding change that would turn every stored URL
into a server-side request forgery vector aimed at whatever network the container
can reach.

**`SESSION_COOKIE_DOMAIN` is never set.** AntennaPod's cookie jar discards a
cookie whose `Domain` does not match the host exactly, and the session cookie is
the only authentication it sends after login. Setting this breaks every
deployment, silently, in a way that looks like bad credentials.

**No endpoint under `/api/` may return 3xx.** A same-origin redirect keeps the
`Authorization` header but rewrites a 301 or 302 into a GET; a cross-scheme one
drops the credentials outright. Both reach the user as "wrong username or
password". There is a test asserting this.

**Credential checks go through `django.contrib.auth.authenticate()`.** Not
`User.objects.get()` plus `check_password()`. Two things depend on it: django-axes
only sees attempts that pass through the authentication backend, and Django's
dummy-hash path for unknown users is what keeps response timing from revealing
which usernames exist.

**Subscriptions belong to the account, not to the device.** The protocol
addresses them per device, and following that literally gives a second phone an
empty list forever — the exact case this server exists to serve. Which device
reported a change is recorded and does not partition what is visible.

**An initial import is a weak claim, and reads serve winners.** A device syncing
from cursor 0 dumps its whole database stamped with the moment of the dump, and
the client applies any downloaded action without consulting its own state — so
imported actions are stored at the epoch sentinel, idempotency is keyed on
content rather than reported moment, and `GET` serves only the current winner of
each (episode, action) group. Storing a dump's fabricated stamps verbatim would
let one stale tablet rewind every device on the account, silently.

**Nothing private reaches the repository or the image.** No hostname, address,
path or credential belonging to any real deployment. `make audit` enforces it and
runs as a pre-commit hook; the git history is permanent, so this matters from the
first commit rather than before release.

## Architecture

`gpodsync/domain/` holds every piece of logic worth testing and imports no Django.
That boundary is what makes the 100% branch-coverage gate on it both achievable
and meaningful — when the gate starts feeling impossible, the cause is usually
logic that has leaked into a view.

`gpodsync/api/base.py` centralises authentication, CSRF exemption, `Origin`
rejection and error shaping, so individual endpoints stay small enough to read in
one screen.

No Django REST Framework, deliberately: its parsers reject the client's
`plain/text` login body, and its `SessionAuthentication` enforces a CSRF token the
client never sends. Working around both leaves less than it costs.

## Tests

Three layers, run separately (`make test-unit`, `-component`, `-acceptance`).

The unit layer carries a hard 100% line-and-branch gate. The component layer is an
endpoint × status-code matrix at 90%. The acceptance layer is measured by its
scenario matrix and deliberately has **no** coverage threshold — gating those on a
percentage rewards tests that touch code over tests that prove behaviour.

`tests/fake_antennapod/` is a replica of the client, pedantic on purpose: it fails
where the Java would fail, including on a response missing `update_urls`, a cookie
carrying a `Domain`, and a `Secure` cookie sent over plain http. It was written
before the server it tests.

## CI

Workflow files are wiring, not logic. Every step is a `make` target, so the thing
CI runs is the thing that runs locally, and a green pipeline cannot mean something
different from a green `make check`.

That is also what makes the pipeline testable with [act](https://github.com/nektos/act)
before it is ever pushed. `act` reproduces triggers, matrices, permissions and
secret plumbing faithfully; it cannot mint OIDC tokens, so keyless signing is not
exercisable locally, and multi-architecture builds under QEMU are too slow there to
be worth it. So the split is deliberate:

- `ci.yml` — audit, lint, types, tests. Fully runnable under `act`, and expected
  to be run that way before pushing.
- `acceptance.yml` — the fake client against the real image. Separate because it
  needs docker, which `act` can only manage with docker-in-docker; keeping it out
  of `ci.yml` is what lets the claim above stay true rather than nearly true.
- `release.yml` — multi-arch build, signing, GHCR push. Deliberately thin, because
  the parts `act` cannot reach should be as close to configuration-only as
  possible. It carries a `workflow_dispatch` trigger so its first real execution
  can be a deliberate dry run rather than a surprise on a tag.

One thing a green `act` run does **not** prove: that the release gate fails when
the `AUDIT_FORBIDDEN_STRINGS` secret is missing. `act` runs with `--bind`, so the
untracked `.env.deploy` is visible inside the container and the audit reads its
list from there — the secret path is never exercised. That guarantee is covered
by `audit.sh --self-test` instead, which runs in every pipeline.

`AUDIT_FORBIDDEN_STRINGS` is exported into `act`'s environment and passed by name,
never as `--secret NAME=value`. An argument is world-readable in
`/proc/<pid>/cmdline`, so spelling it out would publish the private-string list to
`ps`; dotenv-style `--secret-file` is no better, since it mangles the multi-line
value into a single pattern.

## Conventions

Conventional Commits. English for all code, comments, documentation and commit
messages. Self-documenting names over comments; comment the non-obvious *why*,
never the *what*. SOLID and DRY.
