"""The front door at `/`, which replaced a bare 404.

It answers before authentication, so what matters is what it must never do:
redirect, set a cookie, name a deployment, or accept anything but a read.
"""

import pytest
from django.test import Client

pytestmark = [pytest.mark.component, pytest.mark.django_db]


@pytest.fixture
def page():
    return Client().get("/")


class TestTheFrontDoor:
    def test_answers_with_html(self, page):
        assert page.status_code == 200
        assert page["Content-Type"] == "text/html; charset=utf-8"
        assert b"<!doctype html>" in page.content

    def test_shows_the_visitors_own_host(self, page):
        # The host is read client-side, so the same bytes serve every
        # deployment and nothing real is baked into a public artefact.
        assert b"location.host" in page.content

    def test_sets_no_cookie(self, page):
        assert not page.cookies

    def test_makes_no_external_request(self, page):
        # Self-contained by design: no fonts, no scripts, no images from
        # anywhere. The only fetch in the page is the same-origin health probe.
        for marker in (b"https://fonts.", b'src="http', b'href="https://cdn'):
            assert marker not in page.content
        assert b'fetch("/healthz/"' in page.content

    def test_head_works_and_writes_are_refused(self):
        assert Client().head("/").status_code == 200
        assert Client().post("/").status_code == 405

    def test_never_redirects(self):
        # A 3xx here would be harmless to the sync client, which never calls
        # this path — but the no-redirect rule is easier to keep absolute.
        assert Client().get("/").status_code == 200
