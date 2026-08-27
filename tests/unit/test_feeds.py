"""Feed URL validation and the deliberately restrained normalisation."""

import pytest

from gpodsync.domain.feeds import (
    InvalidFeedUrl,
    sanitise_feed_url,
    sanitise_feed_urls,
)

pytestmark = pytest.mark.unit


class TestAccepted:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/feed.xml",
            "http://example.com/feed.xml",
            "https://example.com:8443/feed.xml",
            "https://example.com/feed.xml?format=rss&id=7",
            "https://user:pass@example.com/feed.xml",
        ],
    )
    def test_passes_an_ordinary_url_through_unchanged(self, url):
        assert sanitise_feed_url(url) == url

    def test_a_colon_is_never_escaped(self):
        # AntennaPod unescapes %3A on the way in, working around gpodder.net
        # escaping colons it should have left alone. Producing them would make
        # the client undo our output.
        assert "%3A" not in sanitise_feed_url("https://example.com/feed.xml?t=1:2")


class TestNormalisation:
    def test_lowercases_the_scheme(self):
        assert sanitise_feed_url("HTTPS://example.com/f") == "https://example.com/f"

    def test_lowercases_the_host(self):
        assert sanitise_feed_url("https://Example.COM/f") == "https://example.com/f"

    def test_leaves_the_path_case_alone(self):
        # Paths are case-sensitive; changing one would point at a different feed.
        assert sanitise_feed_url("https://example.com/Feed.XML") == "https://example.com/Feed.XML"

    def test_leaves_credentials_alone(self):
        # Lowercasing the whole authority would lowercase the password too, and
        # break an authenticated feed in a way nobody would attribute to us.
        assert (
            sanitise_feed_url("https://User:PassWord@Example.com/f")
            == "https://User:PassWord@example.com/f"
        )

    def test_strips_surrounding_whitespace(self):
        assert sanitise_feed_url("  https://example.com/f  ") == "https://example.com/f"

    def test_drops_a_fragment(self):
        assert sanitise_feed_url("https://example.com/f#part") == "https://example.com/f"


class TestRejected:
    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "ftp://example.com/f",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://",
            "/relative/path",
            "example.com/feed.xml",
            "https://example.com/\x00",
            "https://example.com/a b",
            "https://example.com/\nfeed",
        ],
    )
    def test_refuses_what_cannot_be_stored(self, url):
        with pytest.raises(InvalidFeedUrl):
            sanitise_feed_url(url)

    def test_refuses_an_overlong_url(self):
        with pytest.raises(InvalidFeedUrl, match="cannot exceed"):
            sanitise_feed_url("https://example.com/" + "a" * 2100)


class TestBatches:
    def test_reports_only_the_urls_that_changed(self):
        result = sanitise_feed_urls(
            ["https://example.com/a", "HTTPS://Example.com/b", "https://example.com/c"]
        )
        assert result.urls == (
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        )
        assert result.update_pairs == (("HTTPS://Example.com/b", "https://example.com/b"),)

    def test_an_unchanged_batch_produces_no_pairs(self):
        # The client rewrites its own subscriptions from these pairs, so an
        # identity pair is not harmless noise — it is an edit to somebody's
        # library that says nothing.
        result = sanitise_feed_urls(["https://example.com/a"])
        assert result.update_pairs == ()

    def test_an_empty_batch_is_fine(self):
        result = sanitise_feed_urls([])
        assert result.urls == ()
        assert result.update_pairs == ()

    def test_one_bad_url_rejects_the_batch(self):
        with pytest.raises(InvalidFeedUrl):
            sanitise_feed_urls(["https://example.com/a", "ftp://example.com/b"])
