"""Configuration reading, which fails closed on purpose.

The recurring theme: every wrong guess this module could make produces a silent
failure rather than a loud one, and silent failures in a self-hosted service
become bug reports nobody can reproduce.
"""

import os
import stat

import pytest

from gpodsync.config import (
    ConfigurationError,
    env_bool,
    env_int,
    env_list,
    env_text,
    resolve_secret_key,
)

pytestmark = pytest.mark.unit


class TestFileBackedValues:
    def test_reads_a_variable(self, monkeypatch):
        monkeypatch.setenv("GPODSYNC_THING", "value")
        assert env_text("GPODSYNC_THING") == "value"

    def test_prefers_the_file_form(self, monkeypatch, tmp_path):
        # How docker secrets arrive: in a file, so the value is not visible in
        # `docker inspect` or a process listing.
        secret = tmp_path / "secret"
        secret.write_text("  from-a-file  ")
        monkeypatch.setenv("GPODSYNC_THING", "from-the-environment")
        monkeypatch.setenv("GPODSYNC_THING_FILE", str(secret))
        assert env_text("GPODSYNC_THING") == "from-a-file"

    def test_an_unreadable_file_is_an_error_not_a_fallback(self, monkeypatch, tmp_path):
        # Falling back to the environment here would start the container with a
        # different secret than the operator intended, and say nothing.
        monkeypatch.setenv("GPODSYNC_THING_FILE", str(tmp_path / "absent"))
        with pytest.raises(ConfigurationError, match="cannot be read"):
            env_text("GPODSYNC_THING")

    def test_returns_the_default_when_unset(self):
        assert env_text("GPODSYNC_DEFINITELY_UNSET", "fallback") == "fallback"


class TestBooleans:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_accepts_the_usual_affirmatives(self, monkeypatch, value):
        monkeypatch.setenv("GPODSYNC_FLAG", value)
        assert env_bool("GPODSYNC_FLAG", default=False) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_accepts_the_usual_negatives(self, monkeypatch, value):
        monkeypatch.setenv("GPODSYNC_FLAG", value)
        assert env_bool("GPODSYNC_FLAG", default=True) is False

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_value_means_unset_not_false(self, monkeypatch, value):
        # `GPODSYNC_LOCKOUT_ENABLED=` is an ordinary thing to find in a compose
        # file, and reading it as false would quietly disable the lockout or the
        # secure-cookie flag. Empty means unset here, as it does for env_int.
        monkeypatch.setenv("GPODSYNC_FLAG", value)
        assert env_bool("GPODSYNC_FLAG", default=True) is True

    def test_a_typo_is_an_error_rather_than_a_silent_false(self, monkeypatch):
        # Reading "ture" as false is the dangerous direction for every switch in
        # this application: cookie security, proxy trust, the lockout.
        monkeypatch.setenv("GPODSYNC_FLAG", "ture")
        with pytest.raises(ConfigurationError, match="true/false"):
            env_bool("GPODSYNC_FLAG", default=True)

    def test_falls_back_when_unset(self):
        assert env_bool("GPODSYNC_DEFINITELY_UNSET", default=True) is True


class TestNumbersAndLists:
    def test_reads_a_number(self, monkeypatch):
        monkeypatch.setenv("GPODSYNC_N", " 42 ")
        assert env_int("GPODSYNC_N", default=1) == 42

    def test_rejects_a_non_number(self, monkeypatch):
        monkeypatch.setenv("GPODSYNC_N", "many")
        with pytest.raises(ConfigurationError, match="whole number"):
            env_int("GPODSYNC_N", default=1)

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_an_empty_value_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("GPODSYNC_N", raw)
        assert env_int("GPODSYNC_N", default=7) == 7

    def test_splits_and_trims_a_list(self, monkeypatch):
        monkeypatch.setenv("GPODSYNC_HOSTS", " a.example.com , b.example.com ,, ")
        assert env_list("GPODSYNC_HOSTS") == ["a.example.com", "b.example.com"]

    def test_an_absent_list_is_the_default(self):
        assert env_list("GPODSYNC_DEFINITELY_UNSET", default=["x"]) == ["x"]


class TestSecretKey:
    def test_the_configured_key_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GPODSYNC_SECRET_KEY", "configured")
        assert resolve_secret_key(tmp_path) == "configured"

    def test_generates_and_persists_when_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GPODSYNC_SECRET_KEY", raising=False)
        generated = resolve_secret_key(tmp_path)
        assert (tmp_path / "secret_key").read_text() == generated

    def test_the_same_key_comes_back_next_time(self, monkeypatch, tmp_path):
        # The whole point. A fresh key per boot invalidates every session, which
        # a user experiences as being signed out at random and reports as "it
        # stops syncing sometimes".
        monkeypatch.delenv("GPODSYNC_SECRET_KEY", raising=False)
        assert resolve_secret_key(tmp_path) == resolve_secret_key(tmp_path)

    def test_the_stored_key_is_not_readable_by_others(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GPODSYNC_SECRET_KEY", raising=False)
        resolve_secret_key(tmp_path)
        mode = stat.S_IMODE((tmp_path / "secret_key").stat().st_mode)
        assert mode == 0o600

    def test_an_unusable_data_directory_is_an_error(self, monkeypatch, tmp_path):
        # A path *underneath a regular file*, which cannot be created whoever you
        # are. The first version of this test revoked write permission instead,
        # and passed here while failing in CI: containers run as root, and root
        # ignores directory permissions. A test that depends on the effective user
        # is testing the user.
        monkeypatch.delenv("GPODSYNC_SECRET_KEY", raising=False)
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        with pytest.raises(ConfigurationError, match="could not be written"):
            resolve_secret_key(blocker / "data")

    def test_an_empty_stored_key_is_replaced(self, monkeypatch, tmp_path):
        # An interrupted write, or a volume pre-created by hand. Returning the
        # empty string as a secret key would be catastrophic and silent.
        monkeypatch.delenv("GPODSYNC_SECRET_KEY", raising=False)
        (tmp_path / "secret_key").write_text("")
        generated = resolve_secret_key(tmp_path)
        assert generated != ""
        assert (tmp_path / "secret_key").read_text() == generated

    def test_a_key_written_by_another_worker_wins(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GPODSYNC_SECRET_KEY", raising=False)
        (tmp_path / "secret_key").write_text("written-by-someone-else")
        assert resolve_secret_key(tmp_path) == "written-by-someone-else"

    def test_an_empty_configured_key_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GPODSYNC_SECRET_KEY", "   ")
        assert len(resolve_secret_key(tmp_path)) > 20


def test_the_secret_key_file_is_not_world_readable_even_if_umask_is_loose(monkeypatch, tmp_path):
    monkeypatch.delenv("GPODSYNC_SECRET_KEY", raising=False)
    previous = os.umask(0)
    try:
        resolve_secret_key(tmp_path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "secret_key").stat().st_mode) == 0o600
