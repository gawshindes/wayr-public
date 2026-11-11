"""Source check: does each configured feed actually deliver stories?

`scripts/find_feeds.py` answers "does this URL parse as a feed". This answers
the question that matters to the pipeline: fetch the feed the way discovery
does, extract the newest few items the way discovery does, and report whether
anything would survive to classification. B1's first prework spent 39 fetches
and extractions on Bloomberg and The Information and got zero words back —
this check would have said so before the run.

Verdicts per feed:
  ok       — items arrive, extraction yields readable text
  blocked  — items arrive but every sampled extraction is empty or thin
             (paywall, bot protection, JS-rendered page)
  stale    — newest item is older than `stale_after_hours`
  empty    — the fetch returned nothing (dead URL, parse failure)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable, List, Optional

from wayr.models import RSSArticle
from wayr.utils import map_concurrent

Extract = Callable[[str], Awaitable[Optional[str]]]
Fetch = Callable[[str], Awaitable[List[RSSArticle]]]


@dataclass
class SourceReport:
    url: str
    verdict: str
    items: int = 0
    newest_hours: Optional[float] = None
    sampled: int = 0
    readable: int = 0  # sampled items at or above min_words
    words: List[int] = field(default_factory=list)
    note: str = ""
    origin: str = "press"  # from the sources file; shown, never judged


def _hours_since(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round((now - dt).total_seconds() / 3600, 1)


async def check_source(
    url: str,
    fetch: Fetch,
    extract: Extract,
    *,
    sample: int = 3,
    min_words: int = 50,
    stale_after_hours: float = 24 * 14,
    now: Optional[datetime] = None,
) -> SourceReport:
    """Fetch one feed, extract its newest `sample` items, and judge it."""
    now = now or datetime.now(timezone.utc)
    try:
        articles = await fetch(url)
    except Exception as e:  # a checker must never die on one bad feed
        return SourceReport(url=url, verdict="empty", note=f"fetch failed: {e}")
    if not articles:
        return SourceReport(url=url, verdict="empty", note="no items")

    articles = sorted(articles, key=lambda a: a.published_date, reverse=True)
    newest = _hours_since(articles[0].published_date, now)
    report = SourceReport(
        url=url, verdict="ok", items=len(articles), newest_hours=newest
    )

    async def _words(article: RSSArticle) -> int:
        # Feeds that carry full text never hit the extractor — same as discovery.
        if article.content and len(article.content.split()) >= min_words:
            return len(article.content.split())
        try:
            text = await extract(str(article.url))
        except Exception:
            text = None
        got = len(text.split()) if text else 0
        feed_words = len((article.feed_content or "").split())
        # discovery falls back to feed text when the page gives too little
        return feed_words if got < min_words <= feed_words else got

    report.words = await map_concurrent(articles[:sample], _words, limit=sample)
    report.sampled = len(report.words)
    report.readable = sum(1 for w in report.words if w >= min_words)

    if report.readable == 0:
        report.verdict = "blocked"
        report.note = "no sampled item extracted readable text"
    elif newest is not None and newest > stale_after_hours:
        report.verdict = "stale"
        report.note = f"newest item {newest / 24:.0f} days old"
    return report


async def check_sources(
    urls: List[str],
    fetch: Fetch,
    extract: Extract,
    *,
    concurrency: int = 4,
    **kw,
) -> List[SourceReport]:
    """Check every feed, a few at a time; results in input order."""
    return await map_concurrent(
        urls, lambda u: check_source(u, fetch, extract, **kw), limit=concurrency
    )


def skipped_report(url: str, origin: str = "press") -> SourceReport:
    """A `type=page` source: accepted in the file, not fetchable yet."""
    return SourceReport(
        url=url, verdict="skipped", origin=origin, note="type=page not supported yet"
    )


def summarize(reports: List[SourceReport]) -> dict:
    counts = {"ok": 0, "blocked": 0, "stale": 0, "empty": 0, "skipped": 0}
    for r in reports:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    return counts
