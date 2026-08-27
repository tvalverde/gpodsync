"""Basic auth decoding, and the failures it deliberately cannot tell apart."""

from base64 import b64encode

import pytest

from gpodsync.domain.credentials import Credentials, decode_basic_auth

pytestmark = pytest.mark.unit


def header_for(raw: str) -> str:
    return "Basic " + b64encode(raw.encode()).decode()


class TestAccepted:
    def test_reads_a_username_and_password(self):
        assert decode_basic_auth(header_for("toni:hunter2")) == Credentials("toni", "hunter2")

    def test_the_scheme_is_case_insensitive(self):
        assert decode_basic_auth(header_for("toni:x").replace("Basic", "bAsIc")) is not None

    def test_a_password_may_contain_colons(self):
        assert decode_basic_auth(header_for("toni:a:b:c")).password == "a:b:c"

    def test_a_password_may_be_empty(self):
        # Empty is a legitimate decode; whether it authenticates is not this
        # function's decision to make.
        assert decode_basic_auth(header_for("toni:")) == Credentials("toni", "")

    def test_accepts_non_ascii(self):
        assert decode_basic_auth(header_for("toñi:contraseña")).username == "toñi"


class TestRejected:
    """Every one of these returns None, and that uniformity is the point.

    A caller that could distinguish them would eventually surface the difference,
    and the login endpoint's job is to answer identically however it failed.
    """

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "Basic",
            "Basic ",
            "Bearer abcdef",
            "Basic !!!not-base64!!!",
            "Basic " + b64encode(b"nocolon").decode(),
            "Basic " + b64encode(b":onlypassword").decode(),
            "Basic " + b64encode(b"\xff\xfe\xfd").decode(),
        ],
    )
    def test_returns_nothing(self, header):
        assert decode_basic_auth(header) is None
