"""That the PRAGMAs actually take effect on a real database file.

`init_command` is a string Django splits on semicolons and runs per connection.
That is behaviour rather than documented API, so a minor upgrade could change it
and silently leave the database in rollback-journal mode — where a reader blocks
the writer, which is the whole reason SQLite is adequate here.

The check runs against a temporary file rather than the test database, which is
in memory and cannot use write-ahead logging at all. Asserting against that would
have been asserting about the test harness.
"""

import pytest
from django.conf import settings
from django.db.backends.sqlite3.base import DatabaseWrapper

# django_db not because these touch the test database — the probe below opens
# its own — but because pytest-django blocks cursor access globally without it.
pytestmark = [pytest.mark.component, pytest.mark.django_db]


@pytest.fixture
def probe(tmp_path):
    configuration = dict(settings.DATABASES["default"])
    configuration["NAME"] = str(tmp_path / "probe.sqlite3")
    configuration.setdefault("ATOMIC_REQUESTS", False)
    configuration.setdefault("AUTOCOMMIT", True)
    configuration.setdefault("CONN_MAX_AGE", 0)
    configuration.setdefault("CONN_HEALTH_CHECKS", False)
    configuration.setdefault("TIME_ZONE", None)

    connection = DatabaseWrapper(configuration, alias="probe")
    yield connection
    connection.close()


def pragma(connection, name: str):
    with connection.cursor() as cursor:
        cursor.execute(f"PRAGMA {name};")
        return cursor.fetchone()[0]


def test_write_ahead_logging_is_on(probe):
    assert str(pragma(probe, "journal_mode")).lower() == "wal"


def test_synchronous_is_normal(probe):
    # 1 is NORMAL: corruption-safe under WAL across an unclean container stop,
    # risking only the last un-checkpointed transactions if the host loses power.
    assert pragma(probe, "synchronous") == 1


def test_transactions_take_the_write_lock_immediately():
    # Deferred mode turns a concurrent write into a "database is locked" that no
    # retry can rescue, because the transaction has already read.
    assert settings.DATABASES["default"]["OPTIONS"]["transaction_mode"] == "IMMEDIATE"
