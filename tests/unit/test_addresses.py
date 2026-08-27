"""Which address the lockout is keyed on.

Wrong in one direction, an attacker claims a fresh address per attempt and the
lockout never fires. Wrong in the other, every client shares the proxy's address
and one attacker locks out the owner. Both are worth a test each.
"""

import pytest

from gpodsync.domain.addresses import client_address

pytestmark = pytest.mark.unit

CLIENT = "203.0.113.10"
PROXY = "10.0.0.1"


class TestWithoutAProxy:
    def test_the_socket_address_is_the_client(self):
        assert client_address(remote_addr=CLIENT, forwarded_for=None, trusted_hops=0) == CLIENT

    def test_a_forwarded_header_is_ignored(self):
        # Nobody verified it, so honouring it would let an attacker pick a new
        # address per attempt and never be locked out at all.
        assert client_address(remote_addr=CLIENT, forwarded_for="1.2.3.4", trusted_hops=0) == CLIENT

    def test_a_negative_hop_count_is_treated_as_none(self):
        assert (
            client_address(remote_addr=CLIENT, forwarded_for="1.2.3.4", trusted_hops=-1) == CLIENT
        )


class TestBehindOneProxy:
    def test_the_last_entry_is_what_the_proxy_saw(self):
        assert client_address(remote_addr=PROXY, forwarded_for=CLIENT, trusted_hops=1) == CLIENT

    def test_entries_the_client_wrote_itself_are_not_believed(self):
        # A client that sends its own X-Forwarded-For simply prepends to it. The
        # rightmost entry is the only one a trusted proxy wrote.
        forged = f"1.1.1.1, 2.2.2.2, {CLIENT}"
        assert client_address(remote_addr=PROXY, forwarded_for=forged, trusted_hops=1) == CLIENT

    def test_whitespace_is_tolerated(self):
        assert (
            client_address(remote_addr=PROXY, forwarded_for=f"  {CLIENT}  ", trusted_hops=1)
            == CLIENT
        )

    def test_a_missing_header_falls_back_to_the_socket(self):
        # Reaching us directly when a proxy was promised. The socket address is
        # the one thing that cannot be forged.
        assert client_address(remote_addr=PROXY, forwarded_for=None, trusted_hops=1) == PROXY

    def test_an_empty_header_falls_back_to_the_socket(self):
        assert client_address(remote_addr=PROXY, forwarded_for="  ,  ", trusted_hops=1) == PROXY


class TestBehindTwoProxies:
    def test_the_second_from_the_right_is_the_client(self):
        chain = f"{CLIENT}, 10.0.0.9"
        assert client_address(remote_addr=PROXY, forwarded_for=chain, trusted_hops=2) == CLIENT

    def test_a_short_chain_falls_back_rather_than_believing_the_client(self):
        # Only one hop present where two were promised: the request did not come
        # the way the operator said it would, so nothing in the header is trusted.
        assert client_address(remote_addr=PROXY, forwarded_for=CLIENT, trusted_hops=2) == PROXY


def test_no_address_at_all():
    assert client_address(remote_addr=None, forwarded_for=None, trusted_hops=0) is None
