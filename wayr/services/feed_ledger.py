"""Feed ledger — every item every feed has ever shown us, and per-feed fetch state.

The pipeline's other caches only remember what we *did* with an article. The
ledger remembers what the feeds *offered*, which is what makes three things
possible that were invisible before: coverage ("of what the feeds offered, what
did we admit?"), feed health (dead or throttled feeds), and conditional GET so
an unchanged feed costs one 304 instead of a full parse.

Shape of ``data/feed_ledger.json``::

    {
      "feeds": {
        "<feed_url>": {
          "etag": str|None, "last_modified": str|None,   # conditional-GET state
          "last_fetched_at": iso, "last_status": 200|304|"error",
          "last_item_count": int, "consecutive_failures": int
        }
      },
      "items": {
        "<url_hash>": {
          "url", "title", "source", "feed_url",
          "rss_published_at": iso,          # source's publish date — freshness key
          "rss_summary": str,               # feed description, tags stripped, <=500 chars
          "first_seen_at": iso, "last_seen_at": iso,
          "admitted": bool, "admitted_at": iso|None
        }
      }
    }

Two rules that are easy to get wrong:

* **The pipeline's delta is "not admitted", not "not seen".** An item the cap
  trimmed off is seen but not admitted, and must stay eligible next run —
  otherwise a capped article is silently lost forever. ``admitted`` is what
  links a ledger row to the pipeline.
* **Retention purges on ``last_seen_at``, not age.** An item purged while still
  in a slow feed's window would be re-admitted as "new" on the next fetch. An
  item is forgotten only once the feed itself has forgotten it.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from wayr.models import RSSArticle
from wayr.utils import get_logger, url_hash

logger = get_logger(__name__)

SUMMARY_MAX_CHARS = 500
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """ISO string, UTC. feedparser dates arrive naive; treat those as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _clean_summary(text: Optional[str]) -> str:
    """Strip tags and collapse whitespace; feed descriptions are often HTML."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()[:SUMMARY_MAX_CHARS]


class FeedLedger:
    """URL-hash-keyed record of feed items plus per-feed HTTP state."""

    def __init__(
        self, ledger_file: str = "data/feed_ledger.json", retention_days: int = 90
    ):
        self.ledger_file = Path(ledger_file)
        self.retention_days = retention_days
        self.feeds: Dict[str, dict] = {}
        self.items: Dict[str, dict] = {}

        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------ io

    def _load(self) -> None:
        if not self.ledger_file.exists():
            return
        try:
            data = json.loads(self.ledger_file.read_text(encoding="utf-8"))
            self.feeds = dict(data.get("feeds") or {})
            self.items = dict(data.get("items") or {})
        except (json.JSONDecodeError, OSError, AttributeError) as e:
            logger.warning(
                f"Feed ledger at {self.ledger_file} unreadable ({e}); starting empty"
            )
            self.feeds, self.items = {}, {}
            return

        purged = self.purge()
        if purged:
            logger.info(
                f"Feed ledger: purged {purged} items unseen for {self.retention_days}d"
            )
        logger.info(
            f"Feed ledger loaded: {len(self.items)} items across {len(self.feeds)} feeds"
        )

    def save(self) -> None:
        """Atomic write: readers see the old file or the new one, never a partial."""
        tmp = self.ledger_file.with_suffix(self.ledger_file.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"feeds": self.feeds, "items": self.items}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.ledger_file)

    # --------------------------------------------------------------- feeds

    def feed_state(self, feed_url: str) -> dict:
        """Conditional-GET state for a feed (empty dict if never fetched)."""
        return dict(self.feeds.get(feed_url) or {})

    def record_fetch(
        self,
        feed_url: str,
        *,
        status: object,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        item_count: int = 0,
    ) -> None:
        """Record the outcome of one fetch. A 200/304 resets the failure counter."""
        prev = self.feeds.get(feed_url) or {}
        ok = status in (200, 304)
        self.feeds[feed_url] = {
            # A 304 carries no new validators; keep the ones we sent.
            "etag": etag if status == 200 else prev.get("etag"),
            "last_modified": (
                last_modified if status == 200 else prev.get("last_modified")
            ),
            "last_fetched_at": _now(),
            "last_status": status,
            "last_item_count": (
                item_count if status == 200 else prev.get("last_item_count", 0)
            ),
            "consecutive_failures": (
                0 if ok else prev.get("consecutive_failures", 0) + 1
            ),
        }

    # --------------------------------------------------------------- items

    def observe(
        self, articles: Iterable[RSSArticle], feed_url: str
    ) -> Tuple[List[RSSArticle], int]:
        """Record every article a feed just showed us.

        Returns ``(never_seen_before, already_known)``. New rows get
        ``first_seen_at``; every row gets ``last_seen_at`` bumped.
        """
        now = _now()
        new: List[RSSArticle] = []
        known = 0
        for article in articles:
            h = url_hash(str(article.url))
            row = self.items.get(h)
            if row is None:
                self.items[h] = {
                    "url": str(article.url),
                    "title": article.title,
                    "source": article.source,
                    "feed_url": feed_url,
                    "source_origin": article.source_origin,
                    "rss_published_at": _iso_utc(article.published_date),
                    "rss_summary": _clean_summary(article.summary),
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "admitted": False,
                    "admitted_at": None,
                }
                new.append(article)
            else:
                row["last_seen_at"] = now
                if article.source_origin and not row.get("source_origin"):
                    row["source_origin"] = article.source_origin  # backfill older rows
                known += 1
        return new, known

    def unadmitted(self, articles: Iterable[RSSArticle]) -> List[RSSArticle]:
        """The pipeline's delta: articles not yet admitted (seen-but-capped included)."""
        return [
            a
            for a in articles
            if not (self.items.get(url_hash(str(a.url))) or {}).get("admitted")
        ]

    def mark_admitted(self, articles: Iterable[RSSArticle]) -> int:
        """Flag articles as handed to the pipeline. Returns how many changed."""
        now = _now()
        changed = 0
        for a in articles:
            row = self.items.get(url_hash(str(a.url)))
            if row is not None and not row.get("admitted"):
                row["admitted"] = True
                row["admitted_at"] = now
                changed += 1
        return changed

    def release(self, articles: Iterable[RSSArticle]) -> int:
        """Undo admission for articles the pipeline could not process.

        Admission means "handed to the pipeline", and it is flagged before
        classification runs. When classification *fails* (a rate limit, a
        timeout) rather than *decides*, the article must go back into the
        delta or a transient error silently deletes it from the pool.
        Returns how many changed.
        """
        changed = 0
        for a in articles:
            row = self.items.get(url_hash(str(a.url)))
            if row is not None and row.get("admitted"):
                row["admitted"] = False
                row.pop("admitted_at", None)
                changed += 1
        return changed

    def purge(self) -> int:
        """Drop items the feeds themselves have stopped showing (by last_seen_at)."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        ).isoformat()
        stale = [
            h
            for h, row in self.items.items()
            if (row.get("last_seen_at") or "") < cutoff
        ]
        for h in stale:
            del self.items[h]
        return len(stale)

    # --------------------------------------------------------------- stats

    def stats(self) -> dict:
        admitted = sum(1 for r in self.items.values() if r.get("admitted"))
        return {
            "feeds": len(self.feeds),
            "items": len(self.items),
            "admitted": admitted,
            "unadmitted": len(self.items) - admitted,
            "failing_feeds": [
                u
                for u, f in self.feeds.items()
                if f.get("consecutive_failures", 0) >= 3
            ],
        }
