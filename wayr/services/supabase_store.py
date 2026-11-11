"""Supabase-backed pipeline persistence.

Writes one row per article to `pipeline_articles`, keyed by url_hash. Each
agent stage calls the matching record_* method to upsert the fields it
produced; earlier-stage columns stay intact and later-stage columns are
populated as the article advances.

The store is a best-effort sink: network errors are logged and swallowed so
a Supabase outage never breaks the pipeline. Dedup and candidate recovery
still run off the on-disk ArticleTracker cache.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from supabase import Client, create_client

from wayr.models import (
    ClassifiedArticle,
    ContentType,
    EditedArticle,
    GeneratedArticle,
    PublishedArticle,
    RSSArticle,
)
from wayr.utils import get_logger, url_hash

logger = get_logger(__name__)

UPSERT_CHUNK = 50  # rows per request; keeps a batch under a few hundred KB
TABLE = "pipeline_articles"
RUNS_TABLE = "newsletter_runs"
POOL_PAGE_SIZE = 1000
POOL_MAX_PAGES = 1000  # hard cap: 1M rows, far beyond any real pool
POOL_STAGES = ("category", "published")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


class SupabasePipelineStore:
    """Stage-by-stage pipeline writer backed by Supabase."""

    def __init__(
        self,
        url: Optional[str],
        secret_key: Optional[str],
        region: Optional[str] = None,
    ):
        self.region = region
        if not url or not secret_key:
            self.client: Optional[Client] = None
            logger.info(
                "Supabase credentials not configured; pipeline_articles writes disabled."
            )
            return
        try:
            self.client = create_client(url, secret_key)
            logger.info(
                f"Supabase pipeline store connected (region={region or 'unset'})."
            )
        except Exception as e:
            self.client = None
            logger.warning(f"Failed to init Supabase client: {e}. Writes disabled.")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ---- internal upsert ----

    def _upsert(self, rows: List[dict]) -> None:
        """Best-effort mirror write, in chunks, each retried once.

        A cold-start discovery batch is 300 rows with 6k chars of content
        each — a single multi-megabyte request, and one TLS hiccup (seen:
        "sslv3 alert bad record mac") lost the whole batch. Chunks bound the
        blast radius; the retry covers the transient.
        """
        client = self.client
        if client is None or not rows:
            return
        for start in range(0, len(rows), UPSERT_CHUNK):
            chunk = rows[start : start + UPSERT_CHUNK]
            for attempt in (1, 2):
                try:
                    client.table(TABLE).upsert(chunk, on_conflict="url_hash").execute()
                    logger.debug(f"Supabase upsert: {len(chunk)} rows into {TABLE}")
                    break
                except Exception as e:
                    msg = str(e)
                    if "column" in msg.lower():
                        # An unknown column rejects the whole batch, not just
                        # that field — the mirror is silently empty until the
                        # migration in scripts/migrations/ is applied.
                        logger.error(
                            f"Supabase upsert failed ({len(chunk)} rows) — a column "
                            f"is missing; apply the latest scripts/migrations/*.sql: {e}"
                        )
                        return  # every chunk would fail the same way
                    if attempt == 1:
                        logger.info(
                            f"Supabase upsert of {len(chunk)} rows failed ({e}); retrying once"
                        )
                        continue
                    logger.warning(
                        f"Supabase upsert failed ({len(chunk)} rows, "
                        f"{start}–{start + len(chunk)} of {len(rows)}): {e}"
                    )

    def fetch_pool(
        self,
        days: int = 7,
        stages: tuple = POOL_STAGES,
        region: Optional[str] = None,
    ) -> List[dict]:
        """Read recent pipeline_articles rows — the story pool as the news
        pipeline left it. Used to seed a local pool for newsletter mode over
        an existing deployment, without touching Modal. Paginated; filtered
        to this store's region unless one is given."""
        client = self.client
        if client is None:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        region = region or self.region
        rows: List[dict] = []
        offset = 0
        for _page in range(POOL_MAX_PAGES):
            q = (
                client.table(TABLE)
                .select("*")
                .gte("created_at", cutoff)
                .in_("last_stage", list(stages))
                .order("created_at", desc=True)
                .range(offset, offset + POOL_PAGE_SIZE - 1)
            )
            if region:
                q = q.eq("region", region)
            page = [r for r in (q.execute().data or []) if isinstance(r, dict)]
            rows.extend(page)
            if len(page) < POOL_PAGE_SIZE:
                break
            offset += POOL_PAGE_SIZE
        else:
            logger.warning(
                f"Supabase pool: hit POOL_MAX_PAGES ({POOL_MAX_PAGES}); result truncated"
            )
        logger.info(
            f"Supabase pool: {len(rows)} rows, last {days}d, stages {list(stages)}"
            + (f", region {region}" if region else "")
        )
        return rows

    def record_newsletter_run(self, run: dict) -> None:
        """Mirror one newsletter run (data/newsletter_runs.json is authoritative)."""
        client = self.client
        if client is None:
            return
        row = {k: v for k, v in run.items() if k not in ("id", "story_hashes")}
        if self.region:
            row["region"] = self.region
        try:
            client.table(RUNS_TABLE).insert(row).execute()
            logger.debug(f"Supabase insert: newsletter run {run.get('status')}")
        except Exception as e:
            logger.warning(f"Supabase newsletter_runs insert failed: {e}")

    # ---- public API: one method per pipeline stage ----

    def record_discovered(self, articles: Iterable[RSSArticle]) -> None:
        """Insert a row for every freshly discovered article.

        Sets last_stage='discovered' and seeds title/source/url/rss_raw/raw_content.
        Safe to call with articles that already exist in the table — upsert
        will refresh raw_content (e.g. after a re-extract) without clobbering
        later-stage columns.
        """
        rows = []
        now = _now()
        for a in articles:
            row = {
                "url_hash": url_hash(str(a.url)),
                "updated_at": now,
                "title": a.title,
                "source_url": str(a.url),
                "source_tag": a.source,
                "source_origin": a.source_origin,
                "rss_published_at": _iso(a.published_date),
                "rss_raw": a.rss_raw,
                "raw_content": a.content,
                "last_stage": "discovered",
                "eligible_at": now,
            }
            if self.region:
                row["region"] = self.region
            rows.append(row)
        self._upsert(rows)

    def record_classification(self, classified: Iterable[ClassifiedArticle]) -> None:
        """Record the outputs of stages A/B/C with appropriate status + last_stage.

        Rejected at A → status='rejected', last_stage='industry'.
        Rejected at B → status='rejected', last_stage='news'.
        Passed C → status='candidate', last_stage='category'.
        """
        rows = []
        now = _now()
        for c in classified:
            orig = c.original
            row = {
                "url_hash": url_hash(str(orig.url)),
                "updated_at": now,
                "industry_classification": c.belongs_to_industry,
            }
            if not c.belongs_to_industry:
                row.update(
                    {
                        "status": "rejected",
                        "last_stage": "industry",
                        "news_classification": None,
                        "article_type": None,
                        "categories": None,
                    }
                )
                rows.append(row)
                continue

            is_news = c.content_type == ContentType.NEWS
            row["news_classification"] = is_news
            row["article_type"] = c.content_type.value

            if not is_news:
                row.update(
                    {"status": "rejected", "last_stage": "news", "categories": None}
                )
                rows.append(row)
                continue

            row.update(
                {
                    "status": "candidate",
                    "last_stage": "category",
                    "categories": list(c.categories),
                }
            )
            rows.append(row)
        self._upsert(rows)

    def record_importance(self, scored: Iterable[ClassifiedArticle]) -> None:
        """Record ImportanceAgent output. Leaves status/last_stage untouched."""
        now = _now()
        rows = [
            {
                "url_hash": url_hash(str(c.original.url)),
                "updated_at": now,
                "importance": {
                    "score": c.importance_score,
                    "reasoning": c.importance_reason or "",
                    "scored_at": now,
                },
            }
            for c in scored
            if c.importance_score is not None
        ]
        self._upsert(rows)

    def record_generated(self, generated: Iterable[GeneratedArticle]) -> None:
        """Record author output. Status stays 'candidate' until publish."""
        rows = []
        now = _now()
        for g in generated:
            orig = g.classified.original
            rows.append(
                {
                    "url_hash": url_hash(str(orig.url)),
                    "updated_at": now,
                    "last_stage": "generated",
                    "author_output": {
                        "title": g.generated_title,
                        "content": g.generated_content,
                        "sources_cited": list(g.sources_cited),
                        "model_used": g.model_used,
                        "generated_at": _iso(g.generated_at),
                    },
                }
            )
        self._upsert(rows)

    def record_edited(self, edited: Iterable[EditedArticle]) -> None:
        """Record proofreader output (also includes the summary-agent summary)."""
        rows = []
        now = _now()
        for e in edited:
            orig = e.generated.classified.original
            rows.append(
                {
                    "url_hash": url_hash(str(orig.url)),
                    "updated_at": now,
                    "last_stage": "edited",
                    "summary_output": {
                        "title": e.edited_title,
                        "content": e.edited_content,
                        "summary": e.summary,
                        "corrected_categories": (
                            list(e.corrected_categories)
                            if e.corrected_categories
                            else None
                        ),
                        "editor_model": e.editor_model,
                        "edited_at": _iso(e.edited_at),
                    },
                }
            )
        self._upsert(rows)

    def record_published(self, published: Iterable[PublishedArticle]) -> None:
        """Mark articles as live on WordPress."""
        rows = []
        now = _now()
        for p in published:
            orig = p.edited.generated.classified.original
            rows.append(
                {
                    "url_hash": url_hash(str(orig.url)),
                    "updated_at": now,
                    "status": "published",
                    "last_stage": "published",
                    "published_at": _iso(p.published_at),
                    "wordpress_post_id": p.wordpress_post_id,
                    "wordpress_url": str(p.wordpress_url),
                }
            )
        self._upsert(rows)
