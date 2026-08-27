"""The case the whole project exists for: a second phone that starts empty.

Subscriptions are account-wide rather than per device. Read literally, the
gpodder protocol gives every device its own list and reconciles them through a
sync-group endpoint AntennaPod never calls — so a second phone syncing for the
first time would receive nothing and stay empty forever.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

pytestmark = [pytest.mark.component, pytest.mark.django_db]

USERNAME = "toni"
PASSWORD = "a-sufficiently-long-passphrase"
FEED_A = "https://example.com/a.xml"
FEED_B = "https://example.com/b.xml"
FEED_C = "https://example.com/c.xml"


@pytest.fixture
def account():
    return get_user_model().objects.create_user(username=USERNAME, password=PASSWORD)


@pytest.fixture
def session(account):
    client = Client()
    client.force_login(account)
    return client


def upload(session, device, add=(), remove=()):
    return session.post(
        f"/api/2/subscriptions/{USERNAME}/{device}.json",
        data=json.dumps({"add": list(add), "remove": list(remove)}),
        content_type="application/json",
    ).json()


def download(session, device, since=0):
    return session.get(f"/api/2/subscriptions/{USERNAME}/{device}.json?since={since}").json()


def test_a_brand_new_phone_receives_everything(session):
    # Phone A has been in use; phone B is a fresh AntennaPod install syncing for
    # the very first time, so it asks from zero.
    upload(session, "phone-a", add=[FEED_A, FEED_B])

    arriving = download(session, "phone-b", since=0)
    assert sorted(arriving["add"]) == sorted([FEED_A, FEED_B])
    assert arriving["remove"] == []


def test_the_device_list_reports_the_shared_count(session):
    upload(session, "phone-a", add=[FEED_A, FEED_B])
    devices = session.get(f"/api/2/devices/{USERNAME}.json").json()
    assert [d["subscriptions"] for d in devices] == [2]

    download(session, "phone-b", since=0)
    session.post(
        f"/api/2/devices/{USERNAME}/phone-b.json",
        data=json.dumps({"caption": "B", "type": "mobile"}),
        content_type="application/json",
    )
    devices = session.get(f"/api/2/devices/{USERNAME}.json").json()
    assert {d["id"]: d["subscriptions"] for d in devices} == {"phone-a": 2, "phone-b": 2}


def test_a_later_subscription_reaches_the_other_phone(session):
    upload(session, "phone-a", add=[FEED_A])
    caught_up = download(session, "phone-b", since=0)["timestamp"]

    upload(session, "phone-a", add=[FEED_C])

    arriving = download(session, "phone-b", since=caught_up)
    assert arriving["add"] == [FEED_C]


def test_an_unsubscribe_reaches_the_other_phone(session):
    upload(session, "phone-a", add=[FEED_A, FEED_B])
    caught_up = download(session, "phone-b", since=0)["timestamp"]

    upload(session, "phone-b", remove=[FEED_A])

    arriving = download(session, "phone-a", since=caught_up)
    assert arriving["remove"] == [FEED_A]
    assert arriving["add"] == []


def test_each_phone_is_told_each_change_once(session):
    seen = []
    cursor = 0
    for feed in (FEED_A, FEED_B, FEED_C):
        upload(session, "phone-a", add=[feed])
        arriving = download(session, "phone-b", since=cursor)
        seen += arriving["add"]
        cursor = arriving["timestamp"]

    assert seen == [FEED_A, FEED_B, FEED_C]
    assert download(session, "phone-b", since=cursor)["add"] == []


def test_a_reinstalled_phone_recovers_what_it_reported_itself(session):
    # Same device name, wiped app. Its own changes are echoed back rather than
    # filtered out, which is the difference between recovering and starting over.
    upload(session, "phone-a", add=[FEED_A, FEED_B])
    assert sorted(download(session, "phone-a", since=0)["add"]) == sorted([FEED_A, FEED_B])
