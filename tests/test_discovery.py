"""Tests for NewsDiscoveryAgent's cap and ledger admission behaviour."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from wayr.agents.discovery import NewsDiscoveryAgent
from wayr.models import RSSArticle
from wayr.services import FeedLedger

FEED = "https://example.com/feed"


def art(n):
    return RSSArticle(
        title=f"Story {n}",
        url=f"https://example.com/{n}",
        source="Example",
        published_date=datetime(2026, 9, 1),
        content="word " * 100,  # already above min_content_words; no extraction
    )


def agent(tmp_path, max_articles, bootstrap_max=300, feed_batch=8):
    ledger = FeedLedger(str(tmp_path / "l.json"))
    a = NewsDiscoveryAgent(
        [FEED], max_articles=max_articles, ledger=ledger, bootstrap_max=bootstrap_max
    )
    batch = [art(i) for i in range(feed_batch)]
    ledger.observe(batch, FEED)  # what the fetcher would have recorded
    a.fetcher.fetch_all = AsyncMock(return_value=batch)
    return a, ledger


@pytest.mark.asyncio
async def test_capped_run_admits_cap_and_leaves_rest_eligible(tmp_path):
    a, ledger = agent(tmp_path, max_articles=3)
    out = await a.discover_articles()
    assert [x.title for x in out] == ["Story 0", "Story 1", "Story 2"]
    assert ledger.stats()["admitted"] == 3
    # next run: the five trimmed ones are still the delta
    assert len(ledger.unadmitted([art(i) for i in range(8)])) == 5


@pytest.mark.asyncio
async def test_uncapped_run_admits_everything(tmp_path):
    a, ledger = agent(tmp_path, max_articles=None)
    out = await a.discover_articles()
    assert len(out) == 8 and ledger.stats()["unadmitted"] == 0


@pytest.mark.asyncio
async def test_uncapped_is_bounded_by_bootstrap_max(tmp_path):
    a, ledger = agent(tmp_path, max_articles=None, bootstrap_max=5)
    assert len(await a.discover_articles()) == 5
    assert ledger.stats()["unadmitted"] == 3


@pytest.mark.asyncio
async def test_second_run_sees_only_the_delta(tmp_path):
    a, ledger = agent(tmp_path, max_articles=None)
    await a.discover_articles()
    assert await a.discover_articles() == []  # everything already admitted


@pytest.mark.asyncio
async def test_ledger_is_persisted_after_admission(tmp_path):
    a, ledger = agent(tmp_path, max_articles=None)
    await a.discover_articles()
    assert FeedLedger(str(tmp_path / "l.json")).stats()["admitted"] == 8


@pytest.mark.asyncio
async def test_stale_items_are_skipped_before_admission(tmp_path):
    """Items past the freshness cap cost nothing and stay unadmitted; items
    without a date pass; the cap applies before the bootstrap ceiling."""
    from datetime import timedelta, timezone

    ledger = FeedLedger(str(tmp_path / "l.json"))
    a = NewsDiscoveryAgent(
        [FEED], max_articles=None, ledger=ledger, max_item_age_hours=96
    )
    now = datetime.now(timezone.utc)
    fresh = [art(i) for i in range(3)]
    for x in fresh:
        x.published_date = now - timedelta(hours=10)
    stale = [art(i) for i in range(3, 6)]
    for x in stale:
        x.published_date = now - timedelta(days=10)
    batch = fresh + stale
    ledger.observe(batch, FEED)
    a.fetcher.fetch_all = AsyncMock(return_value=batch)

    out = await a.discover_articles()
    assert sorted(x.title for x in out) == ["Story 0", "Story 1", "Story 2"]
    assert ledger.stats()["admitted"] == 3
    assert [x.title for x in ledger.unadmitted(batch)] == [
        "Story 3",
        "Story 4",
        "Story 5",
    ]


@pytest.mark.asyncio
async def test_no_age_cap_admits_old_items(tmp_path):
    a, ledger = agent(tmp_path, max_articles=None)  # 2026-09-01 dates, no cap
    assert len(await a.discover_articles()) == 8


@pytest.mark.asyncio
async def test_dedup_batch_dicts_carry_origin(tmp_path):
    from wayr.agents.deduplication import DeduplicationAgent

    a, ledger = agent(tmp_path, max_articles=None, feed_batch=2)
    batch = [art(0), art(1)]
    batch[1].source_origin = "brand"
    a.fetcher.fetch_all = AsyncMock(return_value=batch)
    ledger.observe(batch, FEED)
    a.dedup_agent = AsyncMock(spec=DeduplicationAgent)
    a.dedup_agent.find_duplicates = AsyncMock(
        return_value={"duplicate_groups": [], "batch_duplicates_to_remove": set()}
    )
    await a.discover_articles()
    sent = a.dedup_agent.find_duplicates.await_args_list[0].kwargs["batch_titles"]
    assert [d["origin"] for d in sent] == [None, "brand"]


@pytest.mark.asyncio
async def test_llm_dedup_off_never_calls_the_agent(tmp_path):
    from wayr.agents.deduplication import DeduplicationAgent

    a, ledger = agent(tmp_path, max_articles=None, feed_batch=3)
    a.llm_dedup = False
    a.dedup_agent = AsyncMock(spec=DeduplicationAgent)
    a.dedup_agent.find_duplicates = AsyncMock()
    out = await a.discover_articles()
    assert len(out) == 3
    a.dedup_agent.find_duplicates.assert_not_awaited()


@pytest.mark.asyncio
async def test_feed_text_is_used_when_the_page_refuses(tmp_path):
    ledger = FeedLedger(str(tmp_path / "l.json"))
    a = NewsDiscoveryAgent(
        [FEED], max_articles=None, ledger=ledger, min_content_words=50
    )
    refused = art(1)
    refused.content = None
    refused.feed_content = "feed " * 80  # readable, but not a full post
    fine = art(2)  # already has 100 words of content
    batch = [refused, fine]
    ledger.observe(batch, FEED)
    a.fetcher.fetch_all = AsyncMock(return_value=batch)
    a.extractor.extract = AsyncMock(return_value=None)  # paywall / bot protection
    out = await a.discover_articles()
    assert [x.title for x in out] == ["Story 1", "Story 2"]
    assert out[0].content == refused.feed_content
