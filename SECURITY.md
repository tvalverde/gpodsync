# Security policy

gpodsync is a network service that holds people's podcast subscriptions and
listening history, and it is normally exposed to the internet. Reports are
welcome and taken seriously.

## Reporting a vulnerability

Use GitHub's [private vulnerability
reporting](https://github.com/tvalverde/gpodsync/security/advisories/new) rather
than a public issue.

Please include what an attacker gains, how to reproduce it, and the version or
image digest you tested. A proof of concept helps but is not required.

Expect an acknowledgement within a week. This is a small project maintained by one
person in their own time — there is no bounty, and no guaranteed response time
beyond a genuine intention to fix real problems promptly. If you have heard
nothing after two weeks, please chase it.

## Supported versions

The latest released tag. Fixes go into a new release rather than being backported.

The published image is rebuilt monthly so that base-image and dependency patches
reach it even when the application code has not changed. If you pin a digest,
re-pin periodically.

## Scope

In scope: authentication and session handling, access to another account's data,
injection, resource exhaustion reachable without credentials, secrets reaching
logs or the published image, and supply-chain weaknesses in the build.

Known and accepted, so not vulnerabilities in themselves:

- **The API is CSRF-exempt by necessity.** AntennaPod sends no CSRF token, so no
  amount of design can require one. This is mitigated by a `SameSite` session
  cookie and by rejecting requests that arrive with a browser's `Origin` or
  `Sec-Fetch-Site: cross-site` header. A concrete bypass of *those* is in scope.
- **The admin interface is disabled by default.** Enabling it and exposing it to
  the internet is a deployment choice, and the documentation says not to.
- **Request tracing logs full request and response bodies.** It is off by default,
  and the documentation is explicit about what it captures. Credentials appearing
  in traces *despite* redaction is very much in scope.
- **SQLite with a single worker** is the supported configuration. Concurrency
  problems from running it another way are not.
