"""Article tracking service.

Cache shape (per url_hash):
    {
        "created_at": "<iso>",
        "eligible_at": "<iso>",   # when the story became publishable; = created_at for RSS
        "rss_published_at": "<iso>",  # the source's publish date (UTC) — the freshness key
        "updated_at": "<iso>",
        "title": "...", "url": "...", "source": "...",
        "summary": "<feed description, tags stripped, capped>",  # dedup context
        "last_stage": "discovered"|"industry"|"news"|"category"|"published",
        "industry":  {"belongs": bool, "detected": str|None, "reasoning": str} | None,
        "news_type": {"content_type": str, "reasoning": str}                  | None,
        "category":  {"primary": str, "secondary": str, "reasoning": str}     | None,
        "importance": {"score": float, "reasoning": str, "scored_at": "<iso>"} | None,
        "content": "<extracted text, capped>",   # news-stage entries only; compile-time blurbs
    }

`importance` is written after the category stage and does not advance `last_stage`.

`last_stage` is monotonic — an update only moves forward in the pipeline.
Each classifier's output is persisted to its own sub-dict so filtering
decisions are auditable per-article without re-running the pipeline.
"""

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from wayr.models import RSSArticle, ClassifiedArticle, ContentType
from wayr.utils import clean_summary, get_logger, url_hash

logger = get_logger(__name__)


# Pipeline stages in ascending order. Updates only move forward.
# Extracted content kept on news-stage entries so the newsletter compile step
# can write blurbs without Supabase or a re-fetch. Capped: a blurb needs the
# opening, not the whole article, and this file is loaded every run.
CONTENT_MAX_CHARS = 6000

# Feed summary kept on every entry so cross-batch dedup can show the LLM more
# than a headline for already-published stories.
SUMMARY_MAX_CHARS = 500

STAGE_ORDER = {
    "discovered": 0,
    "industry": 1,  # rejected by industry classifier
    "news": 2,  # passed industry, classified as non-news
    "category": 3,  # passed industry + news, has categories (ready for author)
    "published": 4,  # live on WordPress
}


def _stage_rank(stage: Optional[str]) -> int:
    return STAGE_ORDER.get(stage or "", -1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_iso(dt: datetime) -> str:
    """Normalize to an aware-UTC ISO string so lexicographic comparison is valid."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def entry_from_pipeline_row(row: dict) -> Optional[Tuple[str, dict]]:
    """Map a Supabase pipeline_articles row onto the cache shape above.

    Fields the news pipeline never wrote (eligible_at, rss_published_at,
    importance, content) are filled where the row has them and otherwise fall
    back exactly as get_window_candidates() would. Returns None for rows
    missing a hash or URL.
    """
    h = row.get("url_hash")
    url = row.get("source_url")
    if not h or not url:
        return None
    stage = row.get("last_stage") or "discovered"
    if stage not in STAGE_ORDER:
        stage = "discovered"
    created = row.get("created_at") or _now()
    entry: dict = {
        "created_at": created,
        "eligible_at": row.get("eligible_at") or created,
        "updated_at": row.get("updated_at") or created,
        "title": row.get("title") or "",
        "url": url,
        "source": row.get("source_tag") or "",
        "source_origin": row.get("source_origin"),
        "last_stage": stage,
    }
    if row.get("rss_published_at"):
        entry["rss_published_at"] = row["rss_published_at"]
    if row.get("industry_classification") is not None:
        entry["industry"] = {
            "belongs": bool(row["industry_classification"]),
            "detected": None,
            "reasoning": "",
        }
    news_flag, article_type = row.get("news_classification"), row.get("article_type")
    if news_flag is not None or article_type:
        entry["news_type"] = {
            "content_type": article_type or ("news" if news_flag else "other"),
            "reasoning": "",
        }
    cats = row.get("categories") or []
    if isinstance(cats, list) and cats:
        entry["category"] = {
            "primary": str(cats[0]),
            "secondary": str(cats[1]) if len(cats) > 1 else "",
            "reasoning": "",
        }
    imp = row.get("importance")
    if isinstance(imp, dict) and isinstance(imp.get("score"), (int, float)):
        entry["importance"] = {
            "score": float(imp["score"]),
            "reasoning": imp.get("reasoning") or "",
            "scored_at": imp.get("scored_at") or created,
        }
    if row.get("raw_content"):
        entry["content"] = str(row["raw_content"])[:CONTENT_MAX_CHARS]
    if row.get("wordpress_url"):
        entry["wordpress_url"] = row["wordpress_url"]
    return str(h), entry


class ArticleTracker:
    """Tracks articles through the pipeline. URL-keyed, stage-ordered."""

    def __init__(
        self,
        cache_file: str = "data/processed_articles.json",
        retention_days: int = 30,
    ):
        self.cache_file = Path(cache_file)
        self.retention_days = retention_days
        self.processed: dict = {}

        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_cache()
        self._cleanup_old_entries()

    # ---- persistence ----

    def _load_cache(self) -> None:
        if not self.cache_file.exists():
            logger.info("No existing cache found. Starting fresh.")
            self.processed = {}
            return
        try:
            with open(self.cache_file, "r") as f:
                raw = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load cache file: {e}. Starting fresh.")
            self.processed = {}
            return

        migrated = 0
        for h, data in raw.items():
            if isinstance(data, dict) and "last_stage" in data:
                self.processed[h] = data
            else:
                self.processed[h] = _migrate_legacy_entry(data)
                migrated += 1

        logger.info(
            f"Loaded {len(self.processed)} cache entries"
            + (f" ({migrated} migrated from legacy shape)" if migrated else "")
        )
        if migrated:
            self._save_cache()

    def _save_cache(self) -> None:
        try:
            with open(self.cache_file, "w") as f:
                json.dump(self.processed, f, indent=2)
            logger.debug(f"Saved {len(self.processed)} cache entries")
        except Exception as e:
            logger.error(f"Could not save cache file: {e}")

    def _cleanup_old_entries(self) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        ).isoformat()
        before = len(self.processed)
        self.processed = {
            h: d
            for h, d in self.processed.items()
            if isinstance(d, dict) and d.get("created_at", "") >= cutoff
        }
        removed = before - len(self.processed)
        if removed:
            logger.info(f"Cleaned up {removed} old entries from cache")
            self._save_cache()

    # ---- hashing ----

    def _get_article_hash(self, article: RSSArticle) -> str:
        return url_hash(str(article.url))

    # ---- core write (monotonic stage) ----

    def _update_entry(
        self,
        article: RSSArticle,
        stage: str,
        *,
        industry: Optional[dict] = None,
        news_type: Optional[dict] = None,
        category: Optional[dict] = None,
        importance: Optional[dict] = None,
        content: Optional[str] = None,
    ) -> bool:
        """Create or advance a cache entry.

        Returns True if the entry was created or advanced, False if the
        existing entry is already at or beyond this stage (no-op).
        """
        if stage not in STAGE_ORDER:
            raise ValueError(f"Unknown stage: {stage}")

        h = self._get_article_hash(article)
        existing = self.processed.get(h)
        existing_rank = (
            _stage_rank(existing.get("last_stage"))
            if isinstance(existing, dict)
            else -1
        )
        new_rank = STAGE_ORDER[stage]

        if (
            existing
            and existing_rank >= new_rank
            and not (industry or news_type or category or importance or content)
        ):
            # No stage advance and no new data to merge — skip.
            return False

        entry = (
            existing.copy()
            if isinstance(existing, dict)
            else {
                "created_at": (created := _now()),
                "eligible_at": created,
                "rss_published_at": _utc_iso(article.published_date),
                "title": article.title,
                "url": str(article.url),
                "source": article.source,
                "source_origin": article.source_origin,
                "summary": clean_summary(article.summary, SUMMARY_MAX_CHARS),
            }
        )

        # Stage only moves forward.
        if new_rank > existing_rank:
            entry["last_stage"] = stage

        if industry is not None:
            entry["industry"] = industry
        if news_type is not None:
            entry["news_type"] = news_type
        if category is not None:
            entry["category"] = category
        if importance is not None:
            entry["importance"] = importance
        if content:
            entry["content"] = content[:CONTENT_MAX_CHARS]

        entry["updated_at"] = _now()
        self.processed[h] = entry
        return True

    # ---- public write API ----

    def record_discovered(self, articles: List[RSSArticle]) -> None:
        """Mark fresh articles as seen so we skip them on future runs."""
        count = sum(1 for a in articles if self._update_entry(a, "discovered"))
        if count:
            self._save_cache()
            logger.info(f"Recorded {count} articles at stage=discovered")

    def record_classification(self, classified: List[ClassifiedArticle]) -> None:
        """Persist per-agent classification results.

        Inspects each ClassifiedArticle and writes the sub-dicts for whichever
        stages actually ran (industry → news → category, with early exit).
        """
        count = 0
        for c in classified:
            industry_dict = {
                "belongs": c.belongs_to_industry,
                "detected": c.industry_detected,
                "reasoning": c.industry_reasoning or "",
            }

            if not c.belongs_to_industry:
                # Rejected at A; B and C never ran.
                if self._update_entry(c.original, "industry", industry=industry_dict):
                    count += 1
                continue

            news_type_dict = {
                "content_type": c.content_type.value,
                "reasoning": c.content_type_reason or "",
            }

            if c.content_type != ContentType.NEWS:
                # Passed A, rejected at B; C never ran.
                if self._update_entry(
                    c.original,
                    "news",
                    industry=industry_dict,
                    news_type=news_type_dict,
                ):
                    count += 1
                continue

            # Passed all three stages — has categories.
            primary = c.categories[0] if c.categories else ""
            secondary = c.categories[1] if len(c.categories) > 1 else ""
            category_dict = {
                "primary": primary,
                "secondary": secondary,
                "reasoning": c.classification_reason or "",
            }
            if self._update_entry(
                c.original,
                "category",
                industry=industry_dict,
                news_type=news_type_dict,
                category=category_dict,
                content=c.original.content,
            ):
                count += 1

        if count:
            self._save_cache()
            logger.info(f"Recorded classification for {count} articles")

    def record_importance(self, scored: List[ClassifiedArticle]) -> None:
        """Persist importance scores. Does not advance `last_stage`."""
        now = _now()
        count = 0
        for c in scored:
            if c.importance_score is None:
                continue
            if self._update_entry(
                c.original,
                "category",
                importance={
                    "score": c.importance_score,
                    "reasoning": c.importance_reason or "",
                    "scored_at": now,
                },
            ):
                count += 1
        if count:
            self._save_cache()
            logger.info(f"Recorded importance for {count} articles")

    def record_published(self, articles: List[RSSArticle]) -> None:
        """Mark articles as published on WordPress."""
        count = sum(1 for a in articles if self._update_entry(a, "published"))
        if count:
            self._save_cache()
            logger.info(f"Recorded {count} articles at stage=published")

    # ---- public read API ----

    def mark_duplicates(self, losers_to_keeper: Dict[str, str]) -> int:
        """Record that each loser hash was collapsed into the keeper hash.

        Written by the compile-time dedup pass. The flag is permanent: a story
        judged a duplicate stays out of every later window, so an event that
        shipped once cannot come back as its other outlet's version.
        """
        n = 0
        for loser, keeper in losers_to_keeper.items():
            if loser == keeper:
                continue  # a story is never its own duplicate
            entry = self.processed.get(loser)
            if entry is not None and not entry.get("duplicate_of"):
                entry["duplicate_of"] = keeper
                entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                n += 1
        if n:
            self._save_cache()
            logger.info(f"Marked {n} pool entries as duplicates")
        return n

    def forget_discovered(self, articles: List[RSSArticle]) -> int:
        """Drop entries still at stage=discovered so the URL counts as new again.

        Paired with FeedLedger.release(): an article whose classification
        raised (not one that was rejected) must not be remembered as
        processed. Entries at any later stage are left alone. Returns how
        many were removed.
        """
        removed = 0
        for a in articles:
            h = self._get_article_hash(a)
            entry = self.processed.get(h)
            if entry is not None and entry.get("last_stage") == "discovered":
                del self.processed[h]
                removed += 1
        if removed:
            self._save_cache()
            logger.info(f"Released {removed} article(s) for retry next run")
        return removed

    def is_processed(self, article: RSSArticle) -> bool:
        """True if this URL has been seen at any stage."""
        return self._get_article_hash(article) in self.processed

    def filter_new_articles(self, articles: List[RSSArticle]) -> List[RSSArticle]:
        """Drop articles whose URL is already in the cache."""
        new = [a for a in articles if not self.is_processed(a)]
        duplicates = len(articles) - len(new)
        if duplicates:
            logger.info(f"Filtered out {duplicates} duplicate articles (URL match)")
        logger.info(f"Found {len(new)} new articles to process")
        return new

    def filter_non_news_articles(self, articles: List[RSSArticle]) -> List[RSSArticle]:
        """Drop articles previously classified as off-industry or non-news."""
        kept = []
        filtered = 0
        for article in articles:
            entry = self.processed.get(self._get_article_hash(article))
            if isinstance(entry, dict):
                ind = entry.get("industry") or {}
                nt = entry.get("news_type") or {}
                if ind.get("belongs") is False:
                    filtered += 1
                    continue
                if nt and nt.get("content_type") != "news":
                    filtered += 1
                    continue
            kept.append(article)
        if filtered:
            logger.info(
                f"Filtered out {filtered} known off-industry or non-news articles"
            )
        return kept

    def get_news_candidates(self, max_age_hours: int = 48) -> list:
        """Classified news articles awaiting publication (FIFO order)."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()

        candidates = []
        for h, data in self.processed.items():
            if not isinstance(data, dict):
                continue
            if data.get("last_stage") != "category":
                continue
            if data.get("created_at", "") < cutoff:
                continue
            candidates.append({**data, "url_hash": h})

        candidates.sort(key=lambda x: x.get("created_at", ""))
        if candidates:
            logger.info(f"Found {len(candidates)} news candidates from previous runs")
        return candidates

    def get_unclassified(self, max_age_hours: int = 48) -> list:
        """Entries an earlier run admitted but never classified (last_stage ==
        'discovered'): a container preempted or killed between discovery and the
        classification commit leaves them here, and the feed ledger — which
        marked them admitted — will not offer them again. Oldest first."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat()
        stuck = [
            {**data, "url_hash": h}
            for h, data in self.processed.items()
            if isinstance(data, dict)
            and data.get("last_stage") == "discovered"
            and data.get("created_at", "") >= cutoff
            and not data.get("unreadable")
        ]
        stuck.sort(key=lambda x: x.get("created_at", ""))
        return stuck

    def mark_unreadable(self, url_hashes: List[str], reason: str = "") -> int:
        """A resume attempt found no text to classify (page refused, feed summary
        thin). Remember that, so the entry is not re-fetched on every run; it stays
        in the cache until retention purges it."""
        count = 0
        for h in url_hashes:
            entry = self.processed.get(h)
            if isinstance(entry, dict) and not entry.get("unreadable"):
                entry["unreadable"] = reason or "no readable text"
                entry["updated_at"] = _now()
                count += 1
        if count:
            self._save_cache()
        return count

    def get_window_candidates(
        self,
        window_start: datetime,
        window_end: datetime,
        max_story_age_hours: int,
    ) -> list:
        """Newsletter candidates: classified news in the window, and still fresh.

        Two independent filters, because they answer different questions:
          - `eligible_at` in [window_start, window_end): has an issue already
            covered this story? (the watermark)
          - `rss_published_at` >= window_end - max_age: is it actually news?
            Cold-started, everything in a feed gets eligible_at = now
            regardless of when it published; this keeps week-old stories
            out of "today's" issue.

        Entries written before these fields existed fall back to created_at.
        Sorted by importance score descending; unscored last.
        """
        start = _utc_iso(window_start)
        end = _utc_iso(window_end)
        fresh_cutoff = _utc_iso(window_end - timedelta(hours=max_story_age_hours))

        out = []
        for h, data in self.processed.items():
            # category = classified news; published = the site already ran it
            # (dual mode) — still the day's news for the newsletter.
            if not isinstance(data, dict) or data.get("last_stage") not in (
                "category",
                "published",
            ):
                continue
            if data.get("duplicate_of"):
                continue  # collapsed into another story by a dedup pass
            eligible = data.get("eligible_at") or data.get("created_at", "")
            if not (start <= eligible < end):
                continue
            published = data.get("rss_published_at") or data.get("created_at", "")
            if published < fresh_cutoff:
                continue
            out.append({**data, "url_hash": h})

        def _score(d: dict) -> float:
            imp = d.get("importance") or {}
            s = imp.get("score")
            return float(s) if isinstance(s, (int, float)) else -1.0

        out.sort(key=_score, reverse=True)
        return out

    def import_entries(self, entries: Dict[str, dict], overwrite: bool = False) -> int:
        """Bulk-add entries (e.g. from entry_from_pipeline_row). Existing hashes
        are kept unless `overwrite`. Returns the number written."""
        count = 0
        for h, entry in entries.items():
            if h in self.processed and not overwrite:
                continue
            self.processed[h] = entry
            count += 1
        if count:
            self._save_cache()
            logger.info(f"Imported {count} entries into {self.cache_file}")
        return count

    def get_content(self, url_hash: str) -> str:
        """Persisted extracted content for a news-stage entry, or ""."""
        entry = self.processed.get(url_hash)
        return (entry.get("content") or "") if isinstance(entry, dict) else ""

    def get_recent_titles(self, days: int = 7) -> List[dict]:
        """Titles of recently published articles for cross-batch dedup."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        recent = []
        for h, data in self.processed.items():
            if not isinstance(data, dict):
                continue
            if data.get("last_stage") != "published":
                continue
            if data.get("created_at", "") < cutoff:
                continue
            title = data.get("title", "")
            if title:
                recent.append(
                    {"title": title, "hash": h, "summary": data.get("summary") or ""}
                )

        logger.debug(f"Found {len(recent)} recent published titles for dedup")
        return recent

    # ---- stats / admin ----

    def get_stats(self) -> dict:
        stage_counts: dict = {}
        for d in self.processed.values():
            if isinstance(d, dict):
                s = d.get("last_stage", "unknown")
                stage_counts[s] = stage_counts.get(s, 0) + 1
        return {
            "total": len(self.processed),
            "by_stage": stage_counts,
            "retention_days": self.retention_days,
            "cache_file": str(self.cache_file),
        }

    def reset(self) -> None:
        """Clear all tracked articles (use with caution!)."""
        self.processed = {}
        self._save_cache()
        logger.warning("Article cache has been reset")


def _migrate_legacy_entry(data) -> dict:
    """Coerce a pre-stage-shape cache entry into the new shape.

    Legacy entries had a flat `status` field plus optional `content_type`,
    `categories`, `confidence`, etc. The mapping:
        status=seen                         → last_stage=discovered
        status=classified, ct==news         → last_stage=category (+ news_type + category)
        status=classified, ct!=news         → last_stage=news     (+ news_type, assume industry.belongs=True)
        status=published                    → last_stage=published (preserve any classification sub-data)
    Synthetic confidence fields are dropped.
    """
    if not isinstance(data, dict):
        # Legacy timestamp-only string — treat as discovered with no metadata.
        return {
            "created_at": data if isinstance(data, str) else _now(),
            "updated_at": _now(),
            "title": "",
            "url": "",
            "source": "",
            "last_stage": "discovered",
            "industry": None,
            "news_type": None,
            "category": None,
        }

    status = data.get("status", "seen")
    ct = data.get("content_type")
    cats = data.get("categories") or []

    entry: dict = {
        "created_at": data.get("created_at") or data.get("timestamp") or _now(),
        "updated_at": _now(),
        "title": data.get("title", ""),
        "url": data.get("url", ""),
        "source": data.get("source", ""),
        "industry": None,
        "news_type": None,
        "category": None,
    }

    if status == "published":
        entry["last_stage"] = "published"
    elif status == "classified":
        # Classified entries had passed the industry filter in the old pipeline.
        entry["industry"] = {"belongs": True, "detected": None, "reasoning": ""}
        if ct:
            entry["news_type"] = {"content_type": ct, "reasoning": ""}
        if ct == "news":
            entry["last_stage"] = "category"
            entry["category"] = {
                "primary": cats[0] if cats else "",
                "secondary": cats[1] if len(cats) > 1 else "",
                "reasoning": "",
            }
        else:
            entry["last_stage"] = "news"
    else:
        # "seen" or unknown
        entry["last_stage"] = "discovered"

    return entry
