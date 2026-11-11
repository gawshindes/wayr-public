"""Source check: fetch + extract per feed, verdict per feed."""

from datetime import datetime, timedelta, timezone

import pytest

from wayr.models import RSSArticle
from wayr.services.source_check import check_source, check_sources, summarize

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def art(n, age_hours=1.0, content=""):
    return RSSArticle(
        title=f"Story {n}",
        url=f"https://example.com/{n}",
        source="Example",
        published_date=NOW - timedelta(hours=age_hours),
        content=content,
    )


def fetcher(articles):
    async def fetch(url):
        return articles

    return fetch


def extractor(words_by_url):
    async def extract(url):
        w = words_by_url.get(url)
        return None if w is None else "w " * w

    return extract


@pytest.mark.asyncio
async def test_ok_feed():
    r = await check_source(
        "https://f",
        fetcher([art(1), art(2), art(3), art(4)]),
        extractor(
            {
                "https://example.com/1": 400,
                "https://example.com/2": 300,
                "https://example.com/3": 20,
            }
        ),
        sample=3,
        min_words=50,
        now=NOW,
    )
    assert r.verdict == "ok" and r.items == 4 and r.sampled == 3
    assert r.readable == 2 and r.words == [400, 300, 20]
    assert r.newest_hours == 1.0


@pytest.mark.asyncio
async def test_blocked_when_nothing_extracts():
    r = await check_source(
        "https://f",
        fetcher([art(1), art(2)]),
        extractor({}),
        sample=3,
        min_words=50,
        now=NOW,
    )
    assert r.verdict == "blocked" and r.readable == 0 and r.words == [0, 0]


@pytest.mark.asyncio
async def test_feed_carrying_full_text_needs_no_extraction():
    calls = []

    async def extract(url):
        calls.append(url)
        return None

    r = await check_source(
        "https://f",
        fetcher([art(1, content="w " * 200)]),
        extract,
        min_words=50,
        now=NOW,
    )
    assert r.verdict == "ok" and r.words == [200] and calls == []


@pytest.mark.asyncio
async def test_stale_feed():
    r = await check_source(
        "https://f",
        fetcher([art(1, age_hours=24 * 30)]),
        extractor({"https://example.com/1": 300}),
        stale_after_hours=24 * 14,
        now=NOW,
    )
    assert r.verdict == "stale" and "30 days" in r.note


@pytest.mark.asyncio
async def test_empty_and_failing_fetch():
    assert (
        await check_source("https://f", fetcher([]), extractor({}), now=NOW)
    ).verdict == "empty"

    async def boom(url):
        raise RuntimeError("dns")

    r = await check_source("https://f", boom, extractor({}), now=NOW)
    assert r.verdict == "empty" and "dns" in r.note


@pytest.mark.asyncio
async def test_check_sources_keeps_order_and_summarizes():
    async def fetch(url):
        return [] if url.endswith("dead") else [art(1)]

    reports = await check_sources(
        ["https://a", "https://dead", "https://c"],
        fetch,
        extractor({"https://example.com/1": 300}),
        now=NOW,
    )
    assert [r.verdict for r in reports] == ["ok", "empty", "ok"]
    assert summarize(reports) == {
        "ok": 2,
        "blocked": 0,
        "stale": 0,
        "empty": 1,
        "skipped": 0,
    }


def test_skipped_report_and_summary_count():
    from wayr.services.source_check import skipped_report

    r = skipped_report("https://anthropic.example/news", "brand")
    assert r.verdict == "skipped" and r.origin == "brand" and "not supported" in r.note
    assert summarize([r]) == {
        "ok": 0,
        "blocked": 0,
        "stale": 0,
        "empty": 0,
        "skipped": 1,
    }


@pytest.mark.asyncio
async def test_feed_text_counts_when_the_page_gives_too_little():
    a = art(1)
    a.feed_content = "w " * 120
    r = await check_source(
        "https://f", fetcher([a]), extractor({}), min_words=50, now=NOW
    )
    assert r.verdict == "ok" and r.words == [120]
