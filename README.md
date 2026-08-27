# gpodsync

A small, self-hosted podcast sync server that speaks the gpodder.net API — built
specifically so that **AntennaPod actually syncs against it**.

> Not affiliated with gpodder.net or the gPodder project. The official server is
> [gpodder/mygpo](https://github.com/gpodder/mygpo); this is an independent
> implementation of the subset of its API that AntennaPod uses.

## Why this exists

Several gpodder.net-compatible servers exist, and AntennaPod fails against a
number of them in the same puzzling way: login succeeds, and then every request
afterwards is rejected as unauthorised.

The reason is in AntennaPod's source. It sends `Authorization: Basic` to the login
endpoint **and nowhere else** — its HTTP client only re-attaches credentials to
feed downloads, never to gpodder API calls. So the session cookie the server sets
at login is the *only* thing authenticating everything that follows. And its
cookie jar uses Java's `ACCEPT_ORIGINAL_SERVER` policy, which silently discards a
cookie whose `Domain` attribute does not match the host exactly.

Get that one detail wrong and the server looks fine to every tool except a phone.

gpodsync gets it right, and its test suite includes a deliberately pedantic fake
AntennaPod that fails in exactly the places the real client does — so a regression
shows up in CI rather than on your commute.

## What it implements

The seven API 2 endpoints AntennaPod calls: authentication, device listing and
configuration, subscription changes in both directions, and episode actions in
both directions. Deliberately **not** implemented: the podcast directory, search,
toplists, and public registration.

Subscriptions and listening positions sync between devices. That's the whole
product.

## Quick start

```bash
docker run -d --name gpodsync \
  -v gpodsync-data:/data \
  -e GPODSYNC_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')" \
  -e GPODSYNC_ALLOWED_HOSTS=gpodder.example.com \
  -e GPODSYNC_BOOTSTRAP_USER=you \
  -e GPODSYNC_BOOTSTRAP_PASSWORD=a-long-passphrase \
  -p 8000:8000 \
  ghcr.io/tvalverde/gpodsync:latest
```

`/data` is the only mount it needs. Put a TLS-terminating reverse proxy in front
of it for anything reachable from the internet, and see `.env.example` for the
full set of options.

In AntennaPod: **Settings → Synchronisation → gpodder.net**, choose a self-hosted
server, and enter your host.

### If you serve it over plain HTTP on a LAN

Set `GPODSYNC_SESSION_COOKIE_SECURE=false`. AntennaPod's Java cookie jar will not
send a `Secure` cookie back over `http://`, so leaving it on gives you a login
that succeeds followed by nothing that works — the same failure this project was
written to fix.

## When something does not sync

Set `GPODSYNC_TRACE_REQUESTS=true` and restart. Every request and response is
logged in full, with credentials redacted, so you can see exactly what the phone
sent and what it got back.

Turn it off once you have what you need: the traces contain your complete
listening history and every subscription URL.

## Running it yourself

```bash
make setup   # virtualenv, pinned dependencies, git hooks
make check   # what CI runs: audit, lint, types, and all three test layers
make help    # everything else
```

`docs/protocol.md` documents the wire contract, including the parts where the
client is stricter than the specification suggests.

## Licence

[AGPL-3.0](LICENSE). If you run a modified version as a network service, you must
offer its source to users of that service. The source is at
<https://github.com/tvalverde/gpodsync>.

Security issues: see [SECURITY.md](SECURITY.md).
