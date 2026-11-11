"""News Discovery Agent - Fetches articles from RSS feeds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, TYPE_CHECKING, Union
from wayr.models import RSSArticle, Source
from wayr.services import RSSFetcher, ContentExtractor
from wayr.utils import get_logger, map_concurrent

if TYPE_CHECKING:
    from wayr.services.article_tracker import ArticleTracker
    from wayr.services.feed_ledger import FeedLedger
    from wayr.agents.deduplication import DeduplicationAgent

logger = get_logger(__name__)


def _aware(dt: datetime) -> datetime:
    """Feed dates are normally UTC-aware; treat a naive one as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class NewsDiscoveryAgent:
    """Agent responsible for discovering news from RSS feeds."""

    def __init__(
        self,
        feed_urls: Sequence[Union[str, Source]],
        max_articles: Optional[int] = 10,
        article_tracker: Optional["ArticleTracker"] = None,
        dedup_agent: Optional["DeduplicationAgent"] = None,
        dedup_window_days: int = 7,
        min_content_words: int = 50,
        concurrency: int = 10,
        ledger: Optional["FeedLedger"] = None,
        bootstrap_max: int = 300,
        max_item_age_hours: Optional[int] = None,
        llm_dedup: bool = True,
    ):
        """
        Initialize News Discovery Agent.

        Args:
            feed_urls: The configured sources (`Source` objects or bare URLs)
            max_articles: Maximum number of articles to process per run (not the
                publish limit). ``None`` means uncapped — the newsletter path —
                in which case ``bootstrap_max`` is the only ceiling.
            article_tracker: Optional ArticleTracker for URL-based deduplication and non-news filtering
            dedup_agent: Optional DeduplicationAgent for LLM-based title deduplication
            min_content_words: Drop articles whose extracted content falls below
                this word count — downstream classifiers and the author agent
                won't get a useful signal off a title alone.
            concurrency: Max simultaneous content extractions.
            ledger: Optional FeedLedger. Enables conditional GET in the fetcher and
                makes the pipeline's delta "not yet admitted" rather than "not in
                the tracker".
            bootstrap_max: Hard ceiling per run when uncapped, so a cold ledger
                (first run sees every item in every feed) cannot spike into
                hundreds of extractions at once. Steady state never reaches it.
            max_item_age_hours: Skip items published longer ago than this
                *before* admission, so they stay unadmitted rather than
                spending extraction and three classifier calls on a story the
                compile's freshness cap would drop anyway. Items without a
                feed date pass. ``None`` disables the check (news mode).
            llm_dedup: Run the LLM same-event dedup here, dropping the losers
                before classification. Right for the per-story pipeline (one
                post per event, and the losers are never needed again). Wrong
                for the newsletter: it throws away coverage before anything has
                been scored, and the compile has its own window dedup that
                sees scores and origins. B1's day-2 issue lost Anthropic's Opus
                5.5 launch this way.
        """
        self.name = "NewsDiscoveryAgent"
        self.ledger = ledger
        self.fetcher = RSSFetcher(feed_urls, ledger=ledger)
        self.extractor = ContentExtractor()
        self.max_articles = max_articles
        self.bootstrap_max = bootstrap_max
        self.article_tracker = article_tracker
        self.dedup_agent = dedup_agent
        self.dedup_window_days = dedup_window_days
        self.min_content_words = min_content_words
        self.concurrency = concurrency
        self.max_item_age_hours = max_item_age_hours
        self.llm_dedup = llm_dedup

    async def discover_articles(self) -> List[RSSArticle]:
        """
        Discover and fetch new articles from RSS feeds.

        This method will keep fetching articles until it finds enough NEW ones
        (ones that haven't been processed before), up to max_articles.

        Returns:
            List of NEW RSSArticle objects (deduplicated)
        """
        logger.info("Starting news discovery...")

        # Fetch articles from RSS feeds (no artificial limit)
        # Let caches (published + non-news) do the filtering
        all_articles = await self.fetcher.fetch_all(max_articles=None)

        if not all_articles:
            logger.warning("No articles found from RSS feeds")
            return []

        logger.info(f"Fetched {len(all_articles)} articles from RSS feeds")

        # Item-level delta. With a ledger this is "not yet admitted" — which
        # keeps seen-but-capped items eligible next run. Without one, fall back
        # to the tracker's "never processed".
        if self.ledger:
            candidates = self.ledger.unadmitted(all_articles)
            logger.info(
                f"Ledger delta: {len(candidates)} of {len(all_articles)} not yet admitted"
            )
        else:
            candidates = all_articles

        # Freshness at admission. On a cold ledger "newest N" still reaches
        # back weeks (B1's first run: 106 of 300 admitted items were already
        # past the 96h cap). Those are never useful to the compile, so don't
        # pay to classify them — and don't admit them, so the ledger keeps
        # them out of every later delta too.
        if self.max_item_age_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=self.max_item_age_hours
            )
            before = len(candidates)
            candidates = [
                a
                for a in candidates
                if a.published_date is None or _aware(a.published_date) >= cutoff
            ]
            if before != len(candidates):
                logger.info(
                    f"Skipped {before - len(candidates)} item(s) older than "
                    f"{self.max_item_age_hours}h (left unadmitted)"
                )

        # Filter out duplicates if article tracker is provided
        if self.article_tracker:
            logger.info("Filtering out previously processed articles (URL check)...")
            new_articles = self.article_tracker.filter_new_articles(candidates)

            duplicates_count = len(candidates) - len(new_articles)
            if duplicates_count > 0:
                logger.info(
                    f"Filtered out {duplicates_count} duplicate articles (URL match)"
                )

            # Filter out known non-news articles (cache hit from prior runs).
            logger.info("Filtering out known non-news articles...")
            new_articles = self.article_tracker.filter_non_news_articles(new_articles)
        else:
            new_articles = candidates

        # Cap BEFORE recording — the tracker and the ledger's `admitted` flag
        # only cover articles we actually process. Anything trimmed off here is
        # seen-but-unadmitted and will be re-evaluated on the next run.
        cap = self.max_articles if self.max_articles is not None else self.bootstrap_max
        articles = new_articles[:cap]
        if len(new_articles) > cap:
            reason = (
                "MAX_ARTICLES_PROCESSED_PER_RUN"
                if self.max_articles is not None
                else "DISCOVERY_BOOTSTRAP_MAX"
            )
            logger.info(f"Capped {len(new_articles)} candidates to {cap} ({reason})")

        if self.article_tracker:
            self.article_tracker.record_discovered(articles)
        if self.ledger:
            self.ledger.mark_admitted(articles)
            self.ledger.save()

        if not articles:
            logger.warning(
                "All fetched articles were already admitted, duplicates, or known non-news. No new articles to process."
            )
            return []

        logger.info(f"Discovered {len(articles)} NEW articles")

        # LLM-based title deduplication (within-batch + cross-batch)
        if self.dedup_agent and not self.llm_dedup:
            logger.info(
                "LLM dedup at discovery is off in this mode; the compile's "
                "window dedup collapses same-event stories after scoring"
            )
        elif self.dedup_agent and self.article_tracker:
            articles = await self._llm_deduplicate(articles)
        elif self.dedup_agent:
            # No article tracker, just do within-batch dedup
            articles = await self._llm_deduplicate(articles)

        # Extract full content for articles
        logger.info(f"Extracting full content ({len(articles)} articles)...")

        min_words = self.min_content_words

        async def _extract(article: RSSArticle) -> None:
            if article.content:
                return
            try:
                content = await self.extractor.extract(str(article.url))
                if content:
                    article.content = content
            except Exception as e:
                logger.warning(f"Could not extract content for {article.url}: {e}")
            # The page refused or gave a fragment (paywall, bot protection,
            # Medium): if the feed carried readable text, that beats dropping
            # the story.
            got = len((article.content or "").split())
            feed_words = len((article.feed_content or "").split())
            if got < min_words <= feed_words:
                article.content = article.feed_content
                logger.info(
                    f"Using feed text ({feed_words} words) for '{article.title[:60]}' "
                    f"— page gave {got}"
                )

        await map_concurrent(articles, _extract, limit=self.concurrency)

        # Drop articles whose content is too thin to classify or generate from.
        # RSS feeds often truncate, and extraction can fail (429s, paywalls) —
        # letting a title-only article through would make classifiers guess and
        # the author would hallucinate.
        before = len(articles)
        kept: List[RSSArticle] = []
        dropped_titles: List[str] = []
        for a in articles:
            wc = len((a.content or "").split())
            if wc < self.min_content_words:
                dropped_titles.append(f"{a.title[:80]} ({wc} words)")
            else:
                kept.append(a)
        if dropped_titles:
            logger.info(
                f"Dropped {before - len(kept)} article(s) below "
                f"{self.min_content_words}-word content threshold:"
            )
            for t in dropped_titles:
                logger.info(f"  - {t}")
        articles = kept

        logger.info("News discovery complete")
        return articles

    async def _llm_deduplicate(self, articles: List[RSSArticle]) -> List[RSSArticle]:
        """
        Use LLM-based DeduplicationAgent to remove duplicate stories.

        Two-step process:
        1. Within-batch dedup: compare batch articles against each other
        2. Cross-batch dedup: compare survivors against recently published articles

        Args:
            articles: List of articles to deduplicate

        Returns:
            Deduplicated list of articles
        """
        dedup_agent = self.dedup_agent
        if dedup_agent is None or len(articles) <= 1:
            return articles

        # --- Step 1: Within-batch dedup ---
        logger.info(f"Step 1: Within-batch dedup ({len(articles)} articles)...")

        batch_titles = [
            {
                "index": i,
                "title": article.title,
                "summary": article.summary,
                "origin": article.source_origin,
            }
            for i, article in enumerate(articles)
        ]

        try:
            result = await dedup_agent.find_duplicates(batch_titles=batch_titles)
            indices_to_remove = result.get("batch_duplicates_to_remove", set())

            if indices_to_remove:
                for idx in indices_to_remove:
                    logger.info(
                        f"  Within-batch duplicate: '{articles[idx].title}' ({articles[idx].source})"
                    )

                articles = [
                    article
                    for i, article in enumerate(articles)
                    if i not in indices_to_remove
                ]
                logger.info(
                    f"  Within-batch dedup: kept {len(articles)}, removed {len(indices_to_remove)}"
                )
            else:
                logger.info("  Within-batch dedup: no duplicates found")
        except Exception as e:
            logger.warning(f"  Within-batch dedup failed: {e}. Continuing.")

        # --- Step 2: Cross-batch dedup (survivors vs cache) ---
        if self.article_tracker and len(articles) > 0:
            recent = self.article_tracker.get_recent_titles(days=self.dedup_window_days)
            if recent:
                logger.info(
                    f"Step 2: Cross-batch dedup ({len(articles)} batch vs {len(recent)} cached)..."
                )

                # Re-index survivors
                batch_titles = [
                    {
                        "index": i,
                        "title": article.title,
                        "summary": article.summary,
                        "origin": article.source_origin,
                    }
                    for i, article in enumerate(articles)
                ]

                offset = len(articles)
                cached_titles = [
                    {
                        "index": offset + i,
                        "title": item["title"],
                        "summary": item.get("summary"),
                    }
                    for i, item in enumerate(recent)
                ]

                try:
                    result = await dedup_agent.find_duplicates(
                        batch_titles=batch_titles,
                        cached_titles=cached_titles,
                    )
                    indices_to_remove = result.get("batch_duplicates_to_remove", set())

                    if indices_to_remove:
                        for idx in indices_to_remove:
                            logger.info(
                                f"  Cross-batch duplicate: '{articles[idx].title}' ({articles[idx].source})"
                            )

                        articles = [
                            article
                            for i, article in enumerate(articles)
                            if i not in indices_to_remove
                        ]
                        logger.info(
                            f"  Cross-batch dedup: kept {len(articles)}, removed {len(indices_to_remove)}"
                        )
                    else:
                        logger.info("  Cross-batch dedup: no duplicates found")
                except Exception as e:
                    logger.warning(f"  Cross-batch dedup failed: {e}. Continuing.")
            else:
                logger.info(
                    "Step 2: No cached titles found, skipping cross-batch dedup"
                )

        logger.info(f"LLM dedup complete: {len(articles)} articles remaining")
        return articles
