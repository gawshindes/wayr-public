"""Tests for RSS fetcher service."""

import pytest
from datetime import datetime
from wayr.services import RSSFetcher


@pytest.mark.asyncio
async def test_rss_fetcher_initialization():
    """Test RSSFetcher initialization."""
    feeds = ["https://techcrunch.com/feed/"]
    fetcher = RSSFetcher(feeds)

    assert fetcher.feed_urls == feeds


@pytest.mark.asyncio
async def test_parse_date():
    """Test date parsing."""
    fetcher = RSSFetcher([])

    # Mock entry with parsed date
    entry = {
        "published_parsed": (2024, 1, 15, 10, 30, 0, 0, 0, 0),
    }

    date = fetcher._parse_date(entry)
    assert isinstance(date, datetime)
    assert date.year == 2024
    assert date.month == 1
    assert date.day == 15


@pytest.mark.asyncio
async def test_parse_entry_missing_required_fields():
    """Test parsing entry with missing required fields."""
    fetcher = RSSFetcher([])

    # Entry without title
    entry = {"link": "https://example.com"}
    result = fetcher._parse_entry(entry, "Test Source")
    assert result is None

    # Entry without URL
    entry = {"title": "Test"}
    result = fetcher._parse_entry(entry, "Test Source")
    assert result is None


# Note: Integration tests that fetch real RSS feeds should be run separately
# and may be skipped in CI environments


# --- Conditional GET via the feed ledger ---

from unittest.mock import patch  # noqa: E402

from wayr.services import FeedLedger  # noqa: E402


class _Feed(dict):
    """Minimal stand-in for a feedparser result: attribute + dict access."""

    def __init__(self, status=200, entries=(), etag=None, modified=None):
        super().__init__(etag=etag, modified=modified)
        self.status = status
        self.entries = list(entries)
        self.bozo = False
        self.feed = {"title": "Example"}


class _Resp:
    """Minimal stand-in for a requests response."""

    def __init__(self, status_code=200, content=b"<rss/>", etag=None, modified=None):
        self.status_code = status_code
        self.content = content
        self.headers = {}
        if etag:
            self.headers["ETag"] = etag
        if modified:
            self.headers["Last-Modified"] = modified


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Every fetch goes through requests.get now; never let a test reach the wire."""
    monkeypatch.setattr(
        "wayr.services.rss_fetcher.requests.get", lambda *a, **k: _Resp(200)
    )


def _entry(n):
    return {
        "title": f"Story {n}",
        "link": f"https://example.com/{n}",
        "published": "2026-09-01",
    }


@pytest.mark.asyncio
async def test_304_short_circuits_and_is_recorded(tmp_path):
    ledger = FeedLedger(str(tmp_path / "l.json"))
    fetcher = RSSFetcher(["https://example.com/feed"], ledger=ledger)
    with patch("wayr.services.rss_fetcher.requests.get", return_value=_Resp(304)):
        assert await fetcher.fetch_feed("https://example.com/feed") == []
    assert ledger.feed_state("https://example.com/feed")["last_status"] == 304


@pytest.mark.asyncio
async def test_validators_stored_then_sent(tmp_path):
    ledger = FeedLedger(str(tmp_path / "l.json"))
    url = "https://example.com/feed"
    fetcher = RSSFetcher([url], ledger=ledger)

    first = _Resp(200, etag='"v1"', modified="Mon")
    with patch(
        "wayr.services.rss_fetcher.requests.get", return_value=first
    ) as g, patch(
        "wayr.services.rss_fetcher.feedparser.parse",
        return_value=_Feed(200, [_entry(1), _entry(2)]),
    ):
        articles = await fetcher.fetch_feed(url)
        sent = g.call_args.kwargs["headers"]
        assert "If-None-Match" not in sent and "If-Modified-Since" not in sent
        assert g.call_args.kwargs["timeout"]  # bounded, always
    assert len(articles) == 2
    assert ledger.feed_state(url)["etag"] == '"v1"'
    assert len(ledger.items) == 2  # observed

    with patch("wayr.services.rss_fetcher.requests.get", return_value=_Resp(304)) as g:
        await fetcher.fetch_feed(url)
        sent = g.call_args.kwargs["headers"]
        assert sent["If-None-Match"] == '"v1"' and sent["If-Modified-Since"] == "Mon"


@pytest.mark.asyncio
async def test_http_error_is_recorded_and_raised(tmp_path):
    ledger = FeedLedger(str(tmp_path / "l.json"))
    fetcher = RSSFetcher(["https://example.com/feed"], ledger=ledger)
    with patch("wayr.services.rss_fetcher.requests.get", return_value=_Resp(503)):
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await fetcher.fetch_feed("https://example.com/feed")
    assert ledger.feed_state("https://example.com/feed")["last_status"] == 503


@pytest.mark.asyncio
async def test_timeout_is_recorded_as_error_and_raised(tmp_path):
    import requests

    ledger = FeedLedger(str(tmp_path / "l.json"))
    fetcher = RSSFetcher(["https://example.com/feed"], ledger=ledger)
    with patch(
        "wayr.services.rss_fetcher.requests.get",
        side_effect=requests.ReadTimeout("slow"),
    ):
        with pytest.raises(requests.ReadTimeout):
            await fetcher.fetch_feed("https://example.com/feed")
    assert ledger.feed_state("https://example.com/feed")["last_status"] == "error"


@pytest.mark.asyncio
async def test_no_ledger_is_unchanged_behaviour():
    fetcher = RSSFetcher(["https://example.com/feed"])
    with patch(
        "wayr.services.rss_fetcher.feedparser.parse",
        return_value=_Feed(200, [_entry(1)]),
    ):
        assert len(await fetcher.fetch_feed("https://example.com/feed")) == 1


class TestTypedSources:
    def _feed(self, monkeypatch, fetcher):
        import feedparser

        class Feed:
            status = 200
            bozo = False
            feed = {"title": "OpenAI News"}
            entries = [
                {
                    "title": "GPT-6 Astra",
                    "link": "https://openai.example/index/astra",
                    "published_parsed": (2026, 9, 22, 10, 0, 0, 0, 0, 0),
                }
            ]
            headers = {}

            def get(self, k, d=None):
                return getattr(self, k, d)

        monkeypatch.setattr(feedparser, "parse", lambda *a, **k: Feed())

    @pytest.mark.asyncio
    async def test_articles_carry_feed_url_and_kind(self, monkeypatch):
        from wayr.models import Source

        f = RSSFetcher(
            [
                Source(url="https://openai.example/rss", origin="brand"),
                "https://plain.example/feed",
            ]
        )
        self._feed(monkeypatch, f)
        a = (await f.fetch_feed("https://openai.example/rss"))[0]
        assert a.feed_url == "https://openai.example/rss" and a.source_origin == "brand"
        b = (await f.fetch_feed("https://plain.example/feed"))[0]
        assert b.feed_url == "https://plain.example/feed" and b.source_origin == "press"

    def test_page_sources_are_skipped_with_one_warning(self, caplog):
        from wayr.models import Source

        with caplog.at_level("WARNING"):
            f = RSSFetcher(
                [
                    Source(url="https://a.example/feed"),
                    Source(
                        url="https://anthropic.example/news",
                        type="page",
                        origin="brand",
                    ),
                ]
            )
        assert f.feed_urls == ["https://a.example/feed"]
        assert [s.url for s in f.skipped_sources] == ["https://anthropic.example/news"]
        assert sum("not supported yet" in r.message for r in caplog.records) == 1

    def test_bare_urls_still_work(self):
        f = RSSFetcher(["https://a.example/feed"])
        assert (
            f.feed_urls == ["https://a.example/feed"] and f.sources[0].origin == "press"
        )


class TestFeedCarriedText:
    def _fetcher_with(self, monkeypatch, body_words):
        import feedparser

        class Feed:
            status = 200
            bozo = False
            feed = {"title": "Netflix TechBlog"}
            entries = [
                {
                    "title": "Leave the classpath behind",
                    "link": "https://netflixtechblog.example/post",
                    "published_parsed": (2026, 9, 23, 10, 0, 0, 0, 0, 0),
                    "summary": "<p>teaser</p>",
                    "content": [{"value": "<p>" + "word " * body_words + "</p>"}],
                }
            ]
            headers = {}

            def get(self, k, d=None):
                return getattr(self, k, d)

        monkeypatch.setattr(feedparser, "parse", lambda *a, **k: Feed())
        return RSSFetcher(["https://netflixtechblog.example/feed"])

    @pytest.mark.asyncio
    async def test_full_text_in_feed_becomes_content(self, monkeypatch):
        f = self._fetcher_with(monkeypatch, 250)
        a = (await f.fetch_feed("https://netflixtechblog.example/feed"))[0]
        assert a.content is not None and len(a.content.split()) == 250
        assert a.feed_content == a.content and "<p>" not in a.content

    @pytest.mark.asyncio
    async def test_short_feed_body_is_kept_aside_not_used_as_content(self, monkeypatch):
        f = self._fetcher_with(monkeypatch, 80)
        a = (await f.fetch_feed("https://netflixtechblog.example/feed"))[0]
        assert a.content is None and len(a.feed_content.split()) == 80
