"""Deciding which address a request really came from.

This is the key the brute-force lockout is stored under, so getting it wrong
breaks in one of two directions, both bad. Trust a forwarded header nobody
verified and an attacker picks a fresh address per attempt, walking past the
lockout forever. Ignore it behind a proxy and every client shares the proxy's
address, so one attacker locks out everybody — including the account's owner,
which turns the defence into the attack.

The rule is therefore explicit rather than clever: trust exactly as many
forwarded hops as the operator says are in front of this server, and nothing
else.
"""


def client_address(
    *, remote_addr: str | None, forwarded_for: str | None, trusted_hops: int
) -> str | None:
    """The client's address, given how many proxies are known to be in front.

    `X-Forwarded-For` grows to the right: each proxy appends the address it saw.
    So with one trusted proxy the last entry is what that proxy observed, and
    anything further left was written by whoever is being blocked.
    """
    if trusted_hops <= 0:
        # No proxy in front, so the socket address is the only honest answer and
        # the header is whatever the client felt like claiming.
        return remote_addr

    entries = [entry.strip() for entry in (forwarded_for or "").split(",") if entry.strip()]
    if len(entries) < trusted_hops:
        # Fewer hops than promised: the proxy is misconfigured, or this request
        # reached us directly. Either way the socket address is the one thing
        # that cannot be forged.
        return remote_addr

    return entries[-trusted_hops]
