"""Fixtures for the acceptance suite.

The image is built once and shared; a container is per test, because these
scenarios are about state accumulating and a shared database would let one test's
subscriptions decide another's outcome.
"""

import pytest

from tests.acceptance.containers import build_image, running
from tests.fake_antennapod.client import FakeAntennaPod
from tests.fake_antennapod.http_transport import HttpTransport

USERNAME = "toni"
PASSWORD = "a-sufficiently-long-passphrase"


@pytest.fixture(scope="session")
def image():
    build_image()


@pytest.fixture
def server(image):
    container = running(
        GPODSYNC_BOOTSTRAP_USER=USERNAME,
        GPODSYNC_BOOTSTRAP_PASSWORD=PASSWORD,
    )
    try:
        container.wait_until_healthy()
        yield container
    finally:
        container.remove()


def device(server, name: str) -> FakeAntennaPod:
    """A fresh client with its own cookie jar, as a separate phone would be."""
    client = FakeAntennaPod(base_url=server.base_url, transport=HttpTransport(), device_id=name)
    client.login(USERNAME, PASSWORD)
    return client


@pytest.fixture
def phone(server):
    return device(server, "phone")


@pytest.fixture
def tablet(server):
    return device(server, "tablet")
