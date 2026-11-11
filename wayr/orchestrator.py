"""Orchestrator for WAYR multi-agent system."""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from wayr.config import get_settings
from wayr.agents import (
    NewsDiscoveryAgent,
    IndustryClassifierAgent,
    NewsClassifierAgent,
    CategoryClassifierAgent,
    DeduplicationAgent,
    AuthorAgent,
    ProofreaderAgent,
    SummaryAgent,
    PublishingAgent,
    SocialMediaAgent,
    ImportanceAgent,
    NewsletterAgent,
)
from wayr.models import RSSArticle
from wayr.models.articles import ClassifiedArticle, ContentType
from wayr.models.newsletter import (
    ItemStyle,
    NewsletterIssue,
    NewsletterStory,
    SectionSchema,
    Tier,
)
from wayr.mailers import NewsletterMailer, get_newsletter_mailer
from wayr.services.newsletter_social import public_url, social_copy, wait_until_live
from wayr.publishers import NewsletterPublisher, get_newsletter_publisher
from wayr.services import (
    ContentExtractor,
    NewsletterRenderer,
    NewsletterRun,
    NewsletterRunLog,
    assign_stories,
    compute_window,
    select_stories,
)
from wayr.services.newsletter_selection import score_of
from wayr.utils.text import read_minutes
from wayr.utils import get_logger, map_concurrent, setup_logging, url_hash

logger = get_logger(__name__)


@dataclass
class ResearchResult:
    """What the shared research trunk produces: everything discovered this run,
    every classification outcome (news and non-news alike), and the funnel."""

    discovered: List[RSSArticle] = field(default_factory=list)
    classified: List[ClassifiedArticle] = field(default_factory=list)
    classification_errors: List[Dict[str, str]] = field(default_factory=list)
    industry_rejected: int = 0
    non_news_counts: Dict[str, int] = field(default_factory=dict)
    resumed: int = 0  # admitted by an earlier run, classified in this one

    @property
    def news(self) -> List[ClassifiedArticle]:
        return [c for c in self.classified if c.content_type == ContentType.NEWS]


class WAYROrchestrator:
    """
    Orchestrates the WAYR multi-agent news aggregation pipeline.

    Pipeline Flow:
    1. News Discovery Agent → Fetch articles from RSS feeds
    2. Classification Agent → Hierarchical classification (L1: content type, L2: news category)
    3. Author Agent → Generate AI-powered articles (GPT-4)
    4. Proofreader Agent → Review and edit articles (Claude)
    5. Publishing Agent → Publish to WordPress
    6. Social Media Agent → Post to Twitter/X and other platforms
    """

    def __init__(self):
        """Initialize the orchestrator and all agents."""
        self.settings = get_settings()

        # Setup logging
        setup_logging(self.settings.log_level)

        logger.info("Initializing WAYR Orchestrator...")

        self.uses = self.settings.mode_agents
        logger.info(
            f"Pipeline mode: {self.settings.pipeline_mode} → agents: {sorted(self.uses)}"
        )
        ignored = self.settings.ignored_enable_flags()
        if ignored:
            logger.info(
                f"Not in this mode, so these flags have no effect: {', '.join(ignored)}"
            )

        # Initialize article tracker for deduplication
        from wayr.services import ArticleTracker, FeedLedger, SupabasePipelineStore

        self.article_tracker = ArticleTracker(
            cache_file=self.settings.article_cache_file,
            retention_days=self.settings.article_retention_days,
        )

        # Every item every feed has shown us, plus per-feed conditional-GET
        # state. Prework's delta and coverage measurement both come from here.
        self.feed_ledger = FeedLedger(
            ledger_file=self.settings.feed_ledger_file,
            retention_days=self.settings.feed_ledger_retention_days,
        )

        # Append-only newsletter run log; the compile watermark derives from it.
        # Always constructed (cheap), only written by run_newsletter().
        self.run_log = NewsletterRunLog(log_file=self.settings.newsletter_runs_file)

        # Best-effort Supabase sink for pipeline audit data. Disabled silently
        # if credentials are missing; never raises into the pipeline.
        self.pipeline_store = SupabasePipelineStore(
            url=self.settings.supabase_url,
            secret_key=self.settings.supabase_secret_key,
            region=self.settings.region,
        )

        # Initialize discovery + deduplication agents (optional based on config)
        self.dedup_agent = None
        self.discovery_agent = None
        if self._wants("discovery", self.settings.enable_discovery, "ENABLE_DISCOVERY"):
            if self._wants("dedup"):
                dedup_config = self.settings.get_model_config(self.settings.model_dedup)
                self.dedup_agent = DeduplicationAgent(
                    api_key=dedup_config["api_key"],
                    model=dedup_config["model_name"],
                    provider=dedup_config["provider"],
                )
                logger.info(
                    f"Deduplication Agent: {dedup_config['provider']}:"
                    f"{dedup_config['model_name']}"
                )

            self.discovery_agent = NewsDiscoveryAgent(
                feed_urls=self.settings.sources,
                max_articles=self.settings.discovery_cap,
                article_tracker=self.article_tracker,  # Pass tracker to discovery agent
                dedup_agent=self.dedup_agent,  # Pass LLM dedup agent
                dedup_window_days=self.settings.dedup_window_days,
                min_content_words=self.settings.min_content_words,
                concurrency=self.settings.pipeline_concurrency,
                ledger=self.feed_ledger,
                bootstrap_max=self.settings.discovery_bootstrap_max,
                # Newsletter mode only: there the compile's freshness cap is
                # the sole consumer, so an item past it is never useful. In
                # news and both modes the per-story pipeline keeps its own
                # rules and sees everything.
                max_item_age_hours=(
                    self.settings.newsletter_max_story_age_hours
                    if self.settings.pipeline_mode == "newsletter"
                    else None
                ),
                # Newsletter mode dedups in the compile window instead, after
                # scoring — discovery must not discard coverage first.
                llm_dedup=self.settings.pipeline_mode != "newsletter",
            )

        # Classification agents: each can override the shared model (optional based on config)
        self.industry_classifier_agent = None
        self.news_classifier_agent = None
        self.category_classifier_agent = None
        if self._wants(
            "classification",
            self.settings.enable_classification,
            "ENABLE_CLASSIFICATION",
        ):
            industry_cfg = self.settings.get_classification_model("industry")
            news_cfg = self.settings.get_classification_model("news")
            category_cfg = self.settings.get_classification_model("category")

            self.industry_classifier_agent = IndustryClassifierAgent(
                api_key=industry_cfg["api_key"],
                model=industry_cfg["model_name"],
                provider=industry_cfg["provider"],
                temperature=self.settings.classification_temperature,
                max_tokens=self.settings.classification_max_tokens,
                industry=self.settings.industry,
            )
            self.news_classifier_agent = NewsClassifierAgent(
                api_key=news_cfg["api_key"],
                model=news_cfg["model_name"],
                provider=news_cfg["provider"],
                temperature=self.settings.classification_temperature,
                max_tokens=self.settings.classification_max_tokens,
            )
            self.category_classifier_agent = CategoryClassifierAgent(
                api_key=category_cfg["api_key"],
                model=category_cfg["model_name"],
                provider=category_cfg["provider"],
                temperature=self.settings.classification_temperature,
                max_tokens=self.settings.classification_max_tokens,
                industry=self.settings.industry,
                allowed_categories=self.settings.industry_categories,
            )
            logger.info(
                f"Industry classifier: {industry_cfg['provider']}:{industry_cfg['model_name']}"
            )
            logger.info(
                f"News classifier: {news_cfg['provider']}:{news_cfg['model_name']}"
            )
            logger.info(
                f"Category classifier: {category_cfg['provider']}:{category_cfg['model_name']}"
            )

        # Initialize author agent (optional based on config)
        self.author_agent = None
        if self._wants("author", self.settings.enable_author, "ENABLE_AUTHOR"):
            author_config = self.settings.get_model_config(self.settings.model_author)
            self.author_agent = AuthorAgent(
                api_key=author_config["api_key"],
                model=author_config["model_name"],
                provider=author_config["provider"],
                temperature=self.settings.author_temperature,
                max_tokens=self.settings.author_max_tokens,
                ai_disclosure=self.settings.ai_disclosure_notice,
                add_source_citation=self.settings.add_source_citation,
            )
            logger.info(
                f"Author Agent: {author_config['provider']}:"
                f"{author_config['model_name']}"
            )

        # Initialize proofreader agent (optional based on config)
        self.proofreader_agent = None
        if self._wants(
            "proofreader", self.settings.enable_proofreading, "ENABLE_PROOFREADING"
        ):
            try:
                # Parse proofreader model config
                proofreader_config = self.settings.get_model_config(
                    self.settings.model_proofreader
                )
                self.proofreader_agent = ProofreaderAgent(
                    api_key=proofreader_config["api_key"],
                    model=proofreader_config["model_name"],
                    provider=proofreader_config["provider"],
                    temperature=self.settings.proofreader_temperature,
                    max_tokens=self.settings.proofreader_max_tokens,
                    target_words=self.settings.get_target_words(),
                )
                logger.info(
                    f"Proofreader Agent: {proofreader_config['provider']}:"
                    f"{proofreader_config['model_name']}"
                )
            except ValueError as e:
                logger.warning(
                    f"Failed to initialize proofreader: {e}. Disabling proofreading."
                )
                self.settings.enable_proofreading = False

        # Initialize Summary Agent (optional based on config)
        self.summary_agent = None
        if self._wants("summary", self.settings.enable_summary, "ENABLE_SUMMARY"):
            summary_config = self.settings.get_model_config(self.settings.model_summary)
            self.summary_agent = SummaryAgent(
                api_key=summary_config["api_key"],
                model=summary_config["model_name"],
                provider=summary_config["provider"],
            )
            logger.info(
                f"Summary Agent: {summary_config['provider']}:"
                f"{summary_config['model_name']}"
            )

        # Importance agent — newsletter mode only; mode-gated, no ENABLE flag.
        self.importance_agent: Optional[ImportanceAgent] = None
        if self._wants("importance"):
            importance_cfg = self.settings.get_model_config(
                self.settings.model_importance
            )
            self.importance_agent = ImportanceAgent(
                api_key=importance_cfg["api_key"],
                model=importance_cfg["model_name"],
                provider=importance_cfg["provider"],
                temperature=self.settings.classification_temperature,
                max_tokens=self.settings.classification_max_tokens,
                industry=self.settings.industry,
                criteria=self.settings.newsletter_importance_criteria,
                criteria_file=self.settings.newsletter_importance_criteria_file,
            )
            logger.info(
                f"Importance Agent: {importance_cfg['provider']}:"
                f"{importance_cfg['model_name']} "
                f"(reader criteria: {self.importance_agent.criteria_source})"
            )

        # Newsletter structure: section schema and renderer. Loaded at startup so
        # a bad schema file fails fast, not at compile time.
        self.section_schema: Optional[SectionSchema] = None
        self.renderer: Optional[NewsletterRenderer] = None
        self.newsletter_agent: Optional[NewsletterAgent] = None
        if self._wants("newsletter"):
            self.section_schema = SectionSchema.load(
                self.settings.newsletter_sections_file
            )
            self.renderer = NewsletterRenderer(self.settings.newsletter_template_file)
            nl_cfg = self.settings.get_model_config(self.settings.model_newsletter)
            self.newsletter_agent = NewsletterAgent(
                api_key=nl_cfg["api_key"],
                model=nl_cfg["model_name"],
                provider=nl_cfg["provider"],
                temperature=self.settings.newsletter_temperature,
                max_tokens=self.settings.newsletter_max_tokens,
                voice=self.settings.newsletter_voice,
                voice_file=self.settings.newsletter_voice_file,
            )
            logger.info(
                f"Newsletter Agent: {nl_cfg['provider']}:{nl_cfg['model_name']} "
                f"(voice: {self.newsletter_agent.voice_source})"
            )
            logger.info(
                f"Newsletter structure: {len(self.section_schema.sections)} sections "
                f"({'custom' if self.settings.newsletter_sections_file else 'default'} schema, "
                f"{'custom' if self.settings.newsletter_template_file else 'default'} template)"
            )

        # Newsletter publisher (draft target). Registry lookup fails fast on an
        # unknown name; the settings validator guarantees the credentials.
        self.newsletter_publisher: Optional[NewsletterPublisher] = None
        if self._wants("newsletter_publisher"):
            self.newsletter_publisher = get_newsletter_publisher(self.settings)
            logger.info(f"Newsletter publisher: {self.newsletter_publisher.name}")

        # Newsletter mailer (subscribers' inboxes). None when NEWSLETTER_MAILER=none:
        # the issue then lives only where the publisher put it.
        self.newsletter_mailer: Optional[NewsletterMailer] = None
        if self._wants("newsletter_mailer"):
            self.newsletter_mailer = get_newsletter_mailer(self.settings)
            logger.info(
                f"Newsletter mailer: {self.newsletter_mailer.name}"
                if self.newsletter_mailer
                else "Newsletter mailer: none (NEWSLETTER_MAILER=none)"
            )

        # WordPress publisher. The settings validator guarantees the credentials
        # are present in any mode whose agent set includes it.
        self.publishing_agent: Optional[PublishingAgent] = None
        if self._wants("publishing"):
            self.publishing_agent = PublishingAgent(
                wordpress_url=str(self.settings.wordpress_url),
                wordpress_username=self.settings.wordpress_username or "",
                wordpress_app_password=self.settings.wordpress_app_password or "",
                taxonomy_cache_file=self.settings.wp_taxonomy_cache_file,
            )

        # Initialize Social Media Agent (optional based on config + credentials)
        self.social_media_agent = None
        has_twitter = bool(self.settings.twitter_api_key)
        has_linkedin = bool(
            self.settings.linkedin_access_token
            and self.settings.linkedin_organization_urn
        )

        if not self._wants("social"):
            pass
        elif not self.settings.enable_social_posting:
            logger.info("Social Media Agent disabled (ENABLE_SOCIAL_POSTING=false)")
        elif has_twitter or has_linkedin:
            try:
                self.social_media_agent = SocialMediaAgent(
                    twitter_api_key=self.settings.twitter_api_key,
                    twitter_api_secret=self.settings.twitter_api_secret,
                    twitter_access_token=self.settings.twitter_access_token,
                    twitter_access_token_secret=self.settings.twitter_access_token_secret,
                    linkedin_access_token=self.settings.linkedin_access_token,
                    linkedin_refresh_token=self.settings.linkedin_refresh_token,
                    linkedin_client_id=self.settings.linkedin_client_id,
                    linkedin_client_secret=self.settings.linkedin_client_secret,
                    linkedin_organization_urn=self.settings.linkedin_organization_urn,
                    tracking_file=self.settings.article_cache_file.replace(
                        "processed_articles.json", "twitter_posted.json"
                    ),
                    linkedin_tracking_file=self.settings.article_cache_file.replace(
                        "processed_articles.json", "linkedin_posted.json"
                    ),
                )
                logger.info("Social Media Agent initialized successfully")
            except Exception as e:
                logger.warning(
                    f"Failed to initialize Social Media Agent: {e}. Skipping social posting."
                )
        else:
            logger.info(
                "Social Media Agent disabled (no Twitter or LinkedIn credentials)"
            )

        logger.info("All agents initialized successfully")

    def _wants(self, agent: str, enabled: bool = True, flag: str = "") -> bool:
        """Should this agent be built? In the mode's set AND its flag is on.

        Logs the reason when the answer is no, so a missing agent is never a mystery:
        either the mode excludes it or an operator switched it off.
        """
        if agent not in self.uses:
            logger.info(f"{agent}: not built in {self.settings.pipeline_mode} mode")
            return False
        if not enabled:
            logger.info(f"{agent}: disabled ({flag}=false)")
            return False
        return True

    async def _classify_article(self, article: RSSArticle) -> ClassifiedArticle:
        """Run industry → news → category classifiers with early-exit.

        Emits one INFO log line per agent so filtering decisions are auditable
        per-article in production logs.
        """
        title_snippet = article.title[:80]

        # The classifiers see a capped excerpt when configured; `article`
        # itself (and everything downstream) keeps the full content.
        cap = self.settings.classification_max_content_chars
        view = article
        if cap and article.content and len(article.content) > cap:
            view = article.model_copy(update={"content": article.content[:cap]})

        a = await self.industry_classifier_agent.check(view)
        if not a.belongs_to_industry:
            logger.info(f"[industry] ✗ '{title_snippet}' belongs=False — {a.reasoning}")
            return ClassifiedArticle(
                original=article,
                belongs_to_industry=False,
                industry_reasoning=a.reasoning,
                content_type=ContentType.OTHER,
                content_type_reason="Rejected by industry classifier",
                categories=[],
                classification_reason="Not part of target industry",
            )
        logger.info(f"[industry] ✓ '{title_snippet}' belongs=True")

        b = await self.news_classifier_agent.classify(view)
        if b.content_type != ContentType.NEWS:
            logger.info(
                f"[news] ✗ '{title_snippet}' type={b.content_type.value} — {b.reasoning}"
            )
            return ClassifiedArticle(
                original=article,
                belongs_to_industry=True,
                industry_reasoning=a.reasoning,
                content_type=b.content_type,
                content_type_reason=b.reasoning,
                categories=[],
                classification_reason=f"Non-news ({b.content_type.value})",
            )
        logger.info(f"[news] ✓ '{title_snippet}' type=news")

        c = await self.category_classifier_agent.categorize(view)
        logger.info(
            f"[category] ✓ '{title_snippet}' "
            f"primary={c.primary} secondary={c.secondary}"
        )
        return ClassifiedArticle(
            original=article,
            belongs_to_industry=True,
            industry_reasoning=a.reasoning,
            content_type=ContentType.NEWS,
            content_type_reason=b.reasoning,
            categories=[c.primary, c.secondary],
            classification_reason=c.reasoning,
        )

    async def _revive_unclassified(self, entries: list) -> List[RSSArticle]:
        """Cache entries at stage=discovered → RSSArticles ready to classify.
        The page is re-extracted; the feed summary stands in when the page gives
        too little; an entry with neither is skipped (and stays in the cache)."""
        extractor = ContentExtractor()
        min_words = self.settings.min_content_words

        async def _one(entry: dict) -> Optional[RSSArticle]:
            try:
                content = await extractor.extract(entry["url"])
            except Exception as e:  # noqa: BLE001 - extraction is best effort
                logger.warning(f"Re-extract failed for {entry['url']}: {e}")
                content = None
            summary = entry.get("summary") or ""
            if not content or len(content.split()) < min_words:
                if len(summary.split()) >= min_words:
                    content = summary
                else:
                    logger.warning(
                        f"Resume: no readable text for '{entry.get('title', '?')[:60]}'; skipped"
                    )
                    return None
            published = str(
                entry.get("rss_published_at") or entry.get("created_at") or ""
            )
            if not published:
                return None
            return RSSArticle(
                title=entry.get("title") or entry["url"],
                url=entry["url"],
                source=entry.get("source") or "Unknown",
                published_date=datetime.fromisoformat(published),
                summary=summary or None,
                content=content,
                source_origin=entry.get("source_origin") or "press",
            )

        revived = await map_concurrent(
            entries, _one, limit=self.settings.pipeline_concurrency
        )
        out = [a for a in revived if a is not None]
        unreadable = [e["url_hash"] for e, a in zip(entries, revived) if a is None]
        if unreadable:
            # Not worth fetching again next run; the entry ages out with retention.
            self.article_tracker.mark_unreadable(unreadable)
        logger.info(
            f"Resumed {len(out)} of {len(entries)} with readable text"
            + (
                f"; {len(unreadable)} marked unreadable, not retried"
                if unreadable
                else ""
            )
        )
        return out

    async def _reconstruct_candidates(self, cache_entries: list) -> list:
        """
        Reconstruct ClassifiedArticle objects from cached candidate data.

        Re-extracts article content via HTTP since content is not stored in cache.

        Args:
            cache_entries: List of cache entry dicts from get_news_candidates()

        Returns:
            List of ClassifiedArticle objects ready for the Author agent
        """
        extractor = ContentExtractor()
        reconstructed = []

        for entry in cache_entries:
            try:
                # Re-extract content from the original URL
                url = entry["url"]
                content = await extractor.extract(url)

                if not content:
                    logger.warning(
                        f"Could not re-extract content for candidate: {entry['title'][:50]}..."
                    )
                    continue

                # Reconstruct RSSArticle
                rss_article = RSSArticle(
                    title=entry["title"],
                    url=url,
                    source=entry.get("source", "Unknown"),
                    published_date=datetime.fromisoformat(entry["created_at"]),
                    content=content,
                )

                # Reconstruct ClassifiedArticle from new cache shape.
                industry = entry.get("industry") or {}
                news_type = entry.get("news_type") or {}
                category = entry.get("category") or {}
                cats = [
                    c for c in (category.get("primary"), category.get("secondary")) if c
                ]
                classified = ClassifiedArticle(
                    original=rss_article,
                    belongs_to_industry=industry.get("belongs", True),
                    industry_reasoning=industry.get("reasoning") or None,
                    industry_detected=industry.get("detected"),
                    content_type=ContentType(news_type.get("content_type", "news")),
                    content_type_reason=news_type.get("reasoning", ""),
                    categories=cats,
                    classification_reason=category.get("reasoning", ""),
                )

                reconstructed.append(classified)
                logger.info(f"Reconstructed candidate: {entry['title'][:50]}...")

            except Exception as e:
                logger.warning(
                    f"Failed to reconstruct candidate '{entry.get('title', '?')[:50]}': {e}"
                )
                continue

        return reconstructed

    async def _research(
        self, step: int, total_steps: int
    ) -> Tuple[ResearchResult, int]:
        """The shared research trunk: discovery → classification → persistence.

        Both entry points call this. It deliberately knows nothing about
        candidate recovery, the publish-slot gate, or anything downstream of
        classification — those belong to the per-story pipeline, and keeping
        them out of here is what lets prework see everything the feeds offer.

        Returns the result and the advanced step counter so callers' log
        numbering stays continuous.
        """
        result = ResearchResult()

        if self.discovery_agent is None:
            logger.info(
                "Discovery disabled (ENABLE_DISCOVERY=false); nothing to research"
            )
            return result, step

        # Discover articles (deduplication happens inside discovery agent)
        step += 1
        logger.info(f"\n[Step {step}/{total_steps}] News Discovery")
        logger.info("-" * 60)
        discovered = await self.discovery_agent.discover_articles()
        result.discovered = discovered

        if discovered:
            self.pipeline_store.record_discovered(discovered)
            logger.info(f"✓ Discovered {len(discovered)} new articles")
        else:
            logger.info("No new articles from RSS")

        # Resume what an earlier run admitted but never classified: a container
        # preempted mid-classification leaves entries at stage=discovered, and
        # the ledger — which marked them admitted — will not offer them again.
        # Content was not persisted at that stage, so it is re-extracted.
        fresh = {url_hash(str(a.url)) for a in discovered}
        stuck = [
            e
            for e in self.article_tracker.get_unclassified(
                max_age_hours=self.settings.newsletter_max_story_age_hours
            )
            if e["url_hash"] not in fresh
        ]
        if stuck:
            logger.info(
                f"Resuming {len(stuck)} article(s) an earlier run discovered but "
                f"never classified"
            )
            resumed = await self._revive_unclassified(stuck)
            result.resumed = len(resumed)
            discovered = discovered + resumed

        # Classification (A: industry filter → B: news/non-news → C: categories)
        classified: List[ClassifiedArticle] = []
        classification_errors: List[Dict[str, str]] = []
        a_rejected = 0
        b_nonnews_counts: Dict[str, int] = {}

        if discovered and not self.settings.enable_classification:
            logger.info(
                "\nClassification disabled (ENABLE_CLASSIFICATION=false), treating all discovered articles as news"
            )
            for article in discovered:
                classified.append(
                    ClassifiedArticle(
                        original=article,
                        belongs_to_industry=True,
                        content_type=ContentType.NEWS,
                        content_type_reason="Classification disabled",
                        categories=[],
                        classification_reason="Classification disabled",
                    )
                )
        elif discovered:
            step += 1
            logger.info(
                f"\n[Step {step}/{total_steps}] Classification (Industry → News Type → Categories)"
            )
            logger.info("-" * 60)

            # Fan out the classification chain. Results come back in input
            # order, so `classified` keeps the FIFO ordering the per-story
            # publish cap depends on. Per-article log lines from the three
            # classifiers interleave across articles — each carries its own
            # title snippet, so decisions stay attributable.
            outcomes = await map_concurrent(
                discovered,
                self._classify_article,
                limit=self.settings.pipeline_concurrency,
                return_exceptions=True,
            )

            failed: List[RSSArticle] = []
            for article, outcome in zip(discovered, outcomes):
                if isinstance(outcome, BaseException):
                    classification_errors.append(
                        {"title": article.title, "error": str(outcome)}
                    )
                    logger.error(
                        f"Classification failed for '{article.title}': {outcome}"
                    )
                    failed.append(article)
                    continue

                classified.append(outcome)
                if not outcome.belongs_to_industry:
                    a_rejected += 1
                elif outcome.content_type != ContentType.NEWS:
                    key = outcome.content_type.value
                    b_nonnews_counts[key] = b_nonnews_counts.get(key, 0) + 1

            # A failure is not a decision: hand the article back to the delta
            # so the next run retries it instead of remembering it as done.
            if failed:
                self.feed_ledger.release(failed)
                self.feed_ledger.save()
                self.article_tracker.forget_discovered(failed)
                logger.warning(
                    f"{len(failed)} article(s) failed classification and were "
                    "released for retry next run"
                )

            logger.info(
                f"Classification funnel: {len(discovered)} discovered → "
                f"industry rejected: {a_rejected}, non-news: {b_nonnews_counts}, "
                f"news: {sum(1 for c in classified if c.content_type == ContentType.NEWS)}"
            )

        # Persist per-agent classification results for ALL articles.
        if classified:
            self.article_tracker.record_classification(classified)
            self.pipeline_store.record_classification(classified)

        result.classified = classified
        result.classification_errors = classification_errors
        result.industry_rejected = a_rejected
        result.non_news_counts = b_nonnews_counts
        return result, step

    async def _score_importance(self, article: ClassifiedArticle) -> ClassifiedArticle:
        """Score one news story and attach the result to it."""
        assert self.importance_agent is not None
        result = await self.importance_agent.score(article)
        article.importance_score = result.score
        article.importance_reason = result.reasoning
        flag = "" if result.parsed else " [unparsed]"
        logger.info(
            f"[importance] {result.score:.2f}{flag} '{article.original.title[:70]}' — {result.reasoning[:90]}"
        )
        return article

    async def _importance_step(
        self,
        news: List[ClassifiedArticle],
        results: Dict[str, Any],
        step: int,
        total_steps: int,
    ) -> int:
        """Score `news` for the readers and persist. Shared by prework and the
        dual-mode pipeline, so the pool is filled the same way in both."""
        if self.importance_agent is None or not news:
            return step

        step += 1
        logger.info(f"\n[Step {step}/{total_steps}] Importance Scoring")
        logger.info("-" * 60)
        outcomes = await map_concurrent(
            news,
            self._score_importance,
            limit=self.settings.pipeline_concurrency,
            return_exceptions=True,
        )
        scored: List[ClassifiedArticle] = []
        for article, outcome in zip(news, outcomes):
            if isinstance(outcome, BaseException):
                results["importance_errors"].append(
                    {"title": article.original.title, "error": str(outcome)}
                )
                logger.error(
                    f"Importance scoring failed for '{article.original.title}': {outcome}"
                )
                continue
            scored.append(outcome)

        if scored:
            self.article_tracker.record_importance(scored)
            self.pipeline_store.record_importance(scored)
            feature = sum(1 for c in scored if (c.importance_score or 0) >= 0.6)
            roundup = sum(1 for c in scored if 0.35 <= (c.importance_score or 0) < 0.6)
            logger.info(
                f"Importance: {len(scored)} scored → "
                f"{feature} feature-tier (≥0.6), {roundup} roundup-tier (≥0.35), "
                f"{len(scored) - feature - roundup} below"
            )
        results["scored"] = scored
        return step

    async def run_prework(self) -> dict:
        """Newsletter prework: research only. Never authors, never publishes.

        Discovery → dedup → extract → classify → persist, for everything the
        feeds offer. There is no candidate recovery and no publish-slot gate —
        that gate is precisely the ceiling prework exists to remove. The
        importance score (build item 3) plugs in after research.
        """
        logger.info("=" * 60)
        logger.info("Starting WAYR Prework")
        logger.info("=" * 60)

        results: Dict[str, Any] = {
            "discovered": [],
            "classified": [],
            "news": [],
            "scored": [],
            "classification_errors": [],
            "importance_errors": [],
            "errors": [],
            "ledger": {},
        }

        try:
            total_steps = sum(
                [
                    self.settings.enable_discovery,
                    self.settings.enable_classification,
                    self.importance_agent is not None,
                ]
            )
            research, step = await self._research(step=0, total_steps=total_steps)

            results["discovered"] = research.discovered
            results["classified"] = research.classified
            results["news"] = research.news
            results["classification_errors"] = research.classification_errors

            step = await self._importance_step(
                research.news, results, step, total_steps
            )

            results["ledger"] = self.feed_ledger.stats()

            ledger = results["ledger"]
            logger.info(
                f"\n✓ Prework complete: {len(research.discovered)} discovered, "
                + (f"{research.resumed} resumed, " if research.resumed else "")
                + f"{len(research.classified)} classified, {len(research.news)} news, "
                f"{len(results['scored'])} scored. "
                f"Ledger: {ledger['items']} items across {ledger['feeds']} feeds, "
                f"{ledger['unadmitted']} not yet admitted."
            )
            if ledger["failing_feeds"]:
                logger.warning(f"Feeds failing repeatedly: {ledger['failing_feeds']}")
            return results

        except Exception as e:
            logger.error(f"Prework failed: {e}", exc_info=True)
            results["errors"].append(str(e))
            raise

    async def _content_for(self, story: NewsletterStory) -> str:
        """Source text for a compile-time blurb, without needing Supabase.

        tracker content (persisted at prework) → ledger's RSS summary →
        re-extract → "". Every step is a fallback; none is required.
        """
        text = self.article_tracker.get_content(story.url_hash)
        if text:
            return text
        row = self.feed_ledger.items.get(story.url_hash) or {}
        if row.get("rss_summary"):
            return str(row["rss_summary"])
        try:
            extracted = await ContentExtractor().extract(story.url)
        except Exception as e:  # noqa: BLE001 - any extraction failure is a fallback
            logger.warning(f"Re-extract failed for {story.url}: {e}")
            extracted = None
        return extracted or ""

    async def _dedup_window(self, candidates: list, results: dict) -> list:
        """Collapse same-event candidates; keep the best-scored, mark the rest.

        Losers are flagged `duplicate_of` in the pool so they leave every later
        window too. On any failure the candidates pass through untouched — a
        guard must never empty an issue.
        """
        results["window_duplicates"] = 0
        if self.dedup_agent is None or not candidates:
            return candidates
        # Stories that shipped in recent issues, so a second outlet's write-up
        # of yesterday's story cannot ship today under a new URL. Same rule as
        # the agent's cached members: a group with a shipped story keeps the
        # shipped one and drops every window member.
        # --include-sent puts shipped stories back among the candidates; they
        # must not also appear as cached members or each would match itself.
        in_window = {c["url_hash"] for c in candidates}
        cached = [
            c
            for c in self._recently_shipped_titles(offset=len(candidates))
            if c["url_hash"] not in in_window
        ]
        for i, c in enumerate(cached):  # re-index after the filter
            c["index"] = len(candidates) + i
        if len(candidates) < 2 and not cached:
            return candidates
        batch = [
            {
                "index": i,
                "title": c.get("title", ""),
                "summary": (c.get("summary") or "")[:300],
                "origin": c.get("source_origin"),
            }
            for i, c in enumerate(candidates)
        ]
        try:
            found = await self.dedup_agent.find_duplicates(
                batch_titles=batch, cached_titles=cached or None
            )
        except Exception as e:
            logger.warning(f"Window dedup failed: {e}. Keeping all candidates.")
            return candidates
        groups = found.get("duplicate_groups") or []
        if not groups:
            return candidates
        shipped_by_index = {c["index"]: c["url_hash"] for c in cached}

        def rank(i: int):
            c = candidates[i]
            # the brand's own post first (the primary source the issue should
            # link); then best score; on a tie the earliest report
            return (
                0 if c.get("source_origin") == "brand" else 1,
                -(score_of(c) or 0.0),
                c.get("rss_published_at") or "",
            )

        losers: Dict[str, str] = {}
        for group in groups:
            shipped = [i for i in group if i in shipped_by_index]
            members = sorted((i for i in group if 0 <= i < len(candidates)), key=rank)
            if shipped and members:
                # Already went out in an earlier issue: every window member loses.
                keeper = shipped_by_index[shipped[0]]
                for i in members:
                    losers[candidates[i]["url_hash"]] = keeper
                    logger.info(
                        f"  Window duplicate of a shipped story: "
                        f"'{candidates[i].get('title', '')[:70]}'"
                    )
                continue
            if len(members) < 2:
                continue
            keeper = candidates[members[0]]["url_hash"]
            for i in members[1:]:
                losers[candidates[i]["url_hash"]] = keeper
                logger.info(
                    f"  Window duplicate: '{candidates[i].get('title', '')[:70]}' "
                    f"→ kept '{candidates[members[0]].get('title', '')[:70]}'"
                )
        if not losers:
            return candidates
        self.article_tracker.mark_duplicates(losers)
        results["window_duplicates"] = len(losers)
        logger.info(
            f"Window dedup: collapsed {len(losers)} of {len(candidates)} candidates"
        )
        return [c for c in candidates if c["url_hash"] not in losers]

    def _configured_models(self) -> List[Dict[str, Any]]:
        """Model configs for the roles this mode actually builds (provider, model_name,
        api_key), so a connection test checks what will run and nothing else."""
        s = self.settings
        roles: Dict[str, List[Optional[str]]] = {
            "classification": [
                s.model_classification,
                s.model_classification_industry,
                s.model_classification_news,
                s.model_classification_category,
            ],
            "dedup": [s.model_dedup],
            "importance": [s.model_importance],
            "summary": [s.model_summary],
            "newsletter": [s.model_newsletter],
            "author": [s.model_author],
            "proofreader": [s.model_proofreader if s.enable_proofreading else None],
        }
        out: List[Dict[str, Any]] = []
        for role, specs in roles.items():
            if role not in self.uses:
                continue
            for spec in specs:
                if not spec:
                    continue
                try:
                    out.append(s.get_model_config(spec))
                except ValueError as e:  # a missing key is the test's finding
                    logger.error(f"✗ {role}: {e}")
        return out

    def usage_report(self) -> List[Dict[str, Any]]:
        """Tokens and cost per agent for this process, for the run summary.

        Reasoning tokens are inside output_tokens (OpenAI bills them as
        output); cost is None for models without a price on file.
        """
        from wayr.agents.base import BaseAgent
        from wayr.utils.pricing import estimate_cost_usd

        rows: List[Dict[str, Any]] = []
        for obj in vars(self).values():
            if not isinstance(obj, BaseAgent) or not obj.usage.calls:
                continue
            u = obj.usage
            rows.append(
                {
                    "agent": obj.name,
                    "model": f"{obj.provider}:{obj.model}",
                    **u.as_dict(),
                    "cost_usd": estimate_cost_usd(
                        obj.model,
                        u.input_tokens,
                        u.output_tokens,
                        u.cached_input_tokens,
                    ),
                }
            )
        return rows

    def _recently_shipped_titles(self, offset: int) -> list:
        """Titles of stories that shipped within DEDUP_WINDOW_DAYS, indexed
        from `offset` so they can ride along as the dedup agent's cached
        members. Empty when nothing has shipped yet."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.settings.dedup_window_days)
        ).isoformat()
        out: List[Dict[str, Any]] = []
        for run in self.run_log.successes():
            if (run.get("window_end") or "") < cutoff:
                continue
            for h in run.get("story_hashes") or []:
                entry = self.article_tracker.processed.get(h)
                if not isinstance(entry, dict) or not entry.get("title"):
                    continue
                out.append(
                    {
                        "index": offset + len(out),
                        "title": entry["title"],
                        "summary": (entry.get("summary") or "")[:300],
                        "url_hash": h,
                    }
                )
        return out

    async def _score_unscored(
        self, candidates: List[Dict[str, Any]], results: Dict[str, Any]
    ) -> None:
        """Score window candidates that have no importance yet, in place.

        Makes newsletter mode self-sufficient against a pool filled by a plain
        news-mode pipeline (which never runs the importance agent), or by an
        older deployment. Scores are persisted so calibration runs do not
        re-pay for them. Candidates are re-sorted by score afterwards.
        """
        if self.importance_agent is None:
            return
        pending = [d for d in candidates if score_of(d) is None]
        if not pending:
            return
        logger.info(
            f"Scoring {len(pending)} candidates with no importance yet "
            f"(pool filled outside newsletter prework)"
        )

        async def _one(d: Dict[str, Any]) -> ClassifiedArticle:
            probe = NewsletterStory.from_candidate(d, Tier.FEATURE)
            text = await self._content_for(probe)
            cat = d.get("category") or {}
            when = d.get("rss_published_at") or d.get("created_at") or ""
            try:
                published = datetime.fromisoformat(when)
            except ValueError:
                published = datetime.now(timezone.utc)
            url: Any = d.get("url") or ""  # pydantic coerces to HttpUrl at runtime
            article = RSSArticle(
                title=d.get("title") or "",
                url=url,
                source=d.get("source") or "Unknown",
                published_date=published,
                content=text,
            )
            classified = ClassifiedArticle(
                original=article,
                belongs_to_industry=True,
                content_type=ContentType.NEWS,
                categories=[c for c in (cat.get("primary"), cat.get("secondary")) if c],
                classification_reason=cat.get("reasoning", ""),
            )
            return await self._score_importance(classified)

        outcomes = await map_concurrent(
            pending,
            _one,
            limit=self.settings.pipeline_concurrency,
            return_exceptions=True,
        )
        now = datetime.now(timezone.utc).isoformat()
        scored: List[ClassifiedArticle] = []
        for d, out in zip(pending, outcomes):
            if isinstance(out, BaseException):
                results["importance_errors"].append(
                    {"title": d.get("title", ""), "error": str(out)}
                )
                logger.error(
                    f"Lazy scoring failed for '{d.get('title', '')[:60]}': {out}"
                )
                continue
            d["importance"] = {
                "score": out.importance_score,
                "reasoning": out.importance_reason or "",
                "scored_at": now,
            }
            scored.append(out)
        if scored:
            self.article_tracker.record_importance(scored)
            self.pipeline_store.record_importance(scored)
        results["lazy_scored"] = len(scored)

        def _key(d: Dict[str, Any]) -> float:
            v = score_of(d)
            return v if v is not None else -1.0

        candidates.sort(key=_key, reverse=True)

    async def _blurb_for(self, story: NewsletterStory) -> str:
        assert self.summary_agent is not None
        content = await self._content_for(story)
        if content:
            story.read_minutes = read_minutes(content)
        blurb = await self.summary_agent.generate_summary_from_source(
            story.source_title, content
        )
        if blurb:
            return blurb
        # Nothing to write from (the page refused, the feed carried a line):
        # the feed's own description beats an item with a headline and no text.
        entry = self.article_tracker.processed.get(story.url_hash) or {}
        summary = entry.get("summary") or (
            self.feed_ledger.items.get(story.url_hash) or {}
        ).get("rss_summary")
        if summary:
            logger.info(
                f"Feed summary stands in as the blurb for '{story.source_title[:60]}'"
            )
            return str(summary).strip()[:400]
        return ""

    def plan_newsletter(self) -> Dict[str, Any]:
        """Preflight for a compile run: pure reads, no model calls, nothing recorded.

        Reports what each choice would yield, so the product can ask instead of
        the engine deciding silently — "you missed your last send: only the
        usual stories, or everything since your last issue?" Each option carries
        the `window_hours` to pass to run_newsletter(). Already-shipped stories
        are excluded from both counts, as they would be in the run.
        """
        now = datetime.now(timezone.utc)
        max_age = self.settings.newsletter_max_story_age_hours
        last_end = self.run_log.last_successful_window_end()
        hours_since = (
            round((now - last_end).total_seconds() / 3600, 1) if last_end else None
        )
        sent = self.run_log.sent_hashes()

        def option(window_hours: float) -> Dict[str, Any]:
            start = now - timedelta(hours=window_hours)
            age = max(max_age, math.ceil(window_hours))
            in_window = self.article_tracker.get_window_candidates(start, now, age)
            fresh = [d for d in in_window if d.get("url_hash") not in sent]
            return {
                "window_hours": window_hours,
                "candidates": len(fresh),
                "already_sent": len(in_window) - len(fresh),
                "unscored": sum(1 for d in fresh if score_of(d) is None),
            }

        # "Missed" = a default run would now lose stories: more time has passed
        # since the last issue than a story is allowed to age.
        missed = hours_since is not None and hours_since > max_age
        return {
            "now": now.isoformat(),
            "last_successful_at": last_end.isoformat() if last_end else None,
            "hours_since_last": hours_since,
            "sent_today": self.run_log.has_success_on(now.date()),
            "max_story_age_hours": max_age,
            "missed_schedule": missed,
            "usual": option(float(max_age)),
            "since_last_issue": (
                option(float(math.ceil(hours_since)))
                if hours_since is not None
                else None
            ),
        }

    async def run_newsletter(
        self,
        skip_publishing: bool = False,
        force: bool = False,
        window_hours: Optional[float] = None,
        include_sent: bool = False,
    ) -> dict:
        """Compile the newsletter issue for the current window.

        Window → same-day guard → candidates → selection. Compile, render and
        the Beehiiv push are build items 5–7 and continue from the selection
        returned here. **The watermark advances only when a publish succeeds**
        (item 7 records the run), so this method never writes the run log.

        Args:
            skip_publishing: preview only — never records a run (items 6–7).
            force: bypass the same-day guard and re-run the last window.
            window_hours: explicit window, ignoring the watermark and the same-day
                guard when choosing the window. Side-effect-free with
                skip_publishing; a real publish under it is still recorded. Also lifts the
                freshness cap to the window, since asking for N hours and being
                silently capped at NEWSLETTER_MAX_STORY_AGE_HOURS is a surprise.
            include_sent: also consider stories that already shipped in a
                successful issue (inspection only; normally they are excluded).
        """
        logger.info("=" * 60)
        logger.info("Starting WAYR Newsletter")
        logger.info("=" * 60)

        # Captured once: the window end. Stories landing during this run fall
        # into the next window instead of being skipped by an advanced watermark.
        pipeline_start = datetime.now(timezone.utc)
        s = self.settings

        last_end = self.run_log.last_successful_window_end()
        window_start, window_end = compute_window(
            pipeline_start, last_end, s.newsletter_max_story_age_hours, window_hours
        )
        results: Dict[str, Any] = {
            "status": "selected",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "window_source": (
                "override"
                if window_hours is not None
                else "bootstrap" if last_end is None else "watermark"
            ),
            "candidates": [],
            "featured": [],
            "roundup": [],
            "dropped": [],
            "unscored": [],
            "importance_errors": [],
            "lazy_scored": 0,
            "already_sent": 0,
            "counts": {},
            "sections": [],
            "unplaced": [],
            "issue": None,
            "html": None,
            "compile": {
                "summaries": 0,
                "summary_errors": [],
                "llm_generated": False,
                "compile_error": None,
            },
            "publisher": None,
            "external_id": None,
            "draft_url": None,
            "mail": None,
            "social": None,
            "errors": [],
        }
        logger.info(
            f"Window [{results['window_source']}]: {window_start.isoformat()} → "
            f"{window_end.isoformat()}"
        )

        try:
            if (
                window_hours is None
                and not force
                and self.run_log.has_success_on(pipeline_start.date())
            ):
                logger.info(
                    "An issue already went out today (UTC). Use --force to re-run, "
                    "or --window-hours to preview a window without the guard."
                )
                results["status"] = "already_sent"
                return results

            max_age = s.newsletter_max_story_age_hours
            if window_hours is not None:
                max_age = max(max_age, math.ceil(window_hours))
            in_window = self.article_tracker.get_window_candidates(
                window_start, window_end, max_age
            )
            # Never send a story twice — a per-story guarantee from the run log,
            # independent of whether this window overlaps an earlier one.
            sent = set() if include_sent else self.run_log.sent_hashes()
            candidates = [d for d in in_window if d.get("url_hash") not in sent]
            results["already_sent"] = len(in_window) - len(candidates)
            if results["already_sent"]:
                logger.info(
                    f"Excluded {results['already_sent']} stories that already shipped "
                    f"in an earlier issue"
                )
            results["candidates"] = candidates
            if not candidates:
                logger.info("No candidates in the window. Nothing to compile.")
                results["status"] = "empty"
                return results

            # A pool filled by a news-mode pipeline has no scores yet — score now.
            await self._score_unscored(candidates, results)

            # Same-event guard over the window. Prework's dedup can fail (a
            # rate-limited call is logged and skipped) or miss a pair that
            # arrived in different runs; either way the window is the last
            # place to catch it, and it is small enough for one call.
            candidates = await self._dedup_window(candidates, results)

            selection = select_stories(
                candidates,
                feature_threshold=s.newsletter_feature_threshold,
                roundup_threshold=s.newsletter_roundup_threshold,
                min_stories=s.newsletter_min_stories,
                max_stories=s.newsletter_max_stories,
            )
            results["featured"] = selection.featured
            results["roundup"] = selection.roundup
            results["dropped"] = selection.dropped
            results["unscored"] = selection.unscored
            results["counts"] = selection.counts

            c = selection.counts
            logger.info(
                f"Selected from {len(candidates)} candidates: {c['featured']} featured, "
                f"{c['roundup']} roundup, {c['dropped']} dropped, {c['unscored']} unscored"
                + (
                    f" (backfilled {c['backfilled']} to reach the floor)"
                    if c["backfilled"]
                    else ""
                )
                + (
                    f" (demoted {c['demoted']} to respect the ceiling)"
                    if c["demoted"]
                    else ""
                )
            )
            for tier, items in (
                ("FEATURED", selection.featured),
                ("ROUNDUP", selection.roundup),
            ):
                for d in items:
                    score = (d.get("importance") or {}).get("score")
                    logger.info(
                        f"  [{tier}] {score if score is not None else '-':>4}  {d.get('title', '')[:80]}"
                    )

            # Structure: assign the selected stories into the day's sections.
            if self.section_schema is None or self.renderer is None:
                raise RuntimeError(
                    "run_newsletter needs the newsletter structure agents; "
                    "set PIPELINE_MODE=newsletter"
                )
            specs = self.section_schema.for_day(pipeline_start.weekday())
            featured = [
                NewsletterStory.from_candidate(d, Tier.FEATURE)
                for d in selection.featured
            ]
            roundup = [
                NewsletterStory.from_candidate(d, Tier.ROUNDUP)
                for d in selection.roundup
            ]
            assignment = assign_stories(specs, featured, roundup)
            results["sections"] = [
                {
                    "name": sec.name,
                    "style": sec.style,
                    "placeholder": sec.is_placeholder,
                    "stories": len(sec.stories),
                }
                for sec in assignment.sections
            ]
            results["unplaced"] = [st.source_title for st in assignment.unplaced]
            if assignment.unplaced:
                logger.warning(
                    f"{len(assignment.unplaced)} selected stories had no section with room or a "
                    f"matching tier/category — add a catch-all section: "
                    + "; ".join(st.source_title[:60] for st in assignment.unplaced[:5])
                )
            for sec in assignment.sections:
                logger.info(
                    f"  section '{sec.name}' [{sec.style or 'placeholder'}]: {len(sec.stories)} stories"
                )

            # Blurbs: summarize only the selected stories that render one
            # (feature and brief). quick_hit and link_only never need content.
            needs_blurb = [
                st
                for sec in assignment.sections
                if not sec.is_placeholder
                and sec.spec.style in (ItemStyle.FEATURE, ItemStyle.BRIEF)
                for st in sec.stories
            ]
            if self.summary_agent is not None and needs_blurb:
                logger.info(
                    f"\nSummarizing {len(needs_blurb)} selected stories from source"
                )
                outcomes = await map_concurrent(
                    needs_blurb,
                    self._blurb_for,
                    limit=self.settings.pipeline_concurrency,
                    return_exceptions=True,
                )
                for st, out in zip(needs_blurb, outcomes):
                    if isinstance(out, BaseException):
                        results["compile"]["summary_errors"].append(
                            {"title": st.source_title, "error": str(out)}
                        )
                        logger.error(
                            f"Blurb failed for '{st.source_title[:60]}': {out}"
                        )
                        continue
                    if out:
                        st.blurb = out
                        results["compile"]["summaries"] += 1

            # Deterministic issue first — the always-ships baseline. The compile
            # call below rewrites the title, subject, intro and per-story written
            # fields; if it fails, this ships as-is.
            date_label = (
                f"{pipeline_start:%B} {pipeline_start.day}, {pipeline_start.year}"
            )
            title = f"{s.platform_name} Daily Brief — {date_label}"
            issue = NewsletterIssue(
                title=title,
                subject_line=title,
                intro=(
                    f"The {len(featured)} stories that matter today, plus {len(roundup)} more worth a look."
                    if self.section_schema.intro
                    else ""
                ),
                date_label=date_label,
                sections=assignment.sections,
                generated_at=pipeline_start,
                llm_generated=False,
                platform_name=s.platform_name,
                ai_disclosure_notice=s.ai_disclosure_notice,
            )
            # Compile: one LLM call for the editorial layer.
            if self.newsletter_agent is not None and NewsletterAgent.payload(issue):
                logger.info("\nCompiling the issue (subject, intro, headlines, emoji)")
                readers = (
                    self.importance_agent.criteria if self.importance_agent else ""
                )
                try:
                    issue = await self.newsletter_agent.compile(
                        issue, readers=readers, intro=self.section_schema.intro
                    )
                except Exception as e:  # noqa: BLE001 - fallback is the contract
                    logger.error(
                        f"Compile failed ({e}); shipping the deterministic issue",
                        exc_info=True,
                    )
                    results["compile"]["compile_error"] = str(e)
            results["compile"]["llm_generated"] = issue.llm_generated

            html = self.renderer.render(issue)
            results["issue"] = issue
            results["html"] = html
            logger.info(
                f"Rendered issue: {issue.story_count} stories across "
                f"{len(issue.sections)} sections, {len(html)} bytes of HTML"
            )

            if skip_publishing:
                logger.info(
                    "Preview only (--skip-publishing): no draft pushed, run not recorded"
                )
                results["status"] = "preview"
                return results

            if self.newsletter_publisher is None:
                raise RuntimeError(
                    "No newsletter publisher configured; set PIPELINE_MODE=newsletter"
                )

            # Publish the draft. The watermark advances ONLY on success — a
            # failed run is recorded so it is auditable, but leaves the window
            # to be re-read next time.
            pub = self.newsletter_publisher
            logger.info(f"\nPushing draft to {pub.name}")
            logger.info("-" * 60)
            try:
                published = await pub.publish(issue, html)
            except Exception as e:  # noqa: BLE001 - recorded, not re-raised
                run = NewsletterRun(
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                    status="failed",
                    story_count=issue.story_count,
                    story_hashes=[
                        st.url_hash for sec in issue.sections for st in sec.stories
                    ],
                    publisher=pub.name,
                    error=str(e)[:500],
                )
                self.run_log.record(run)
                self.pipeline_store.record_newsletter_run(run.to_dict())
                logger.error(f"✗ Draft push failed: {e}")
                results["status"] = "failed"
                results["publisher"] = pub.name
                results["errors"].append(str(e))
                return results

            run = NewsletterRun(
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                status="success",
                story_count=issue.story_count,
                story_hashes=[
                    st.url_hash for sec in issue.sections for st in sec.stories
                ],
                publisher=pub.name,
                external_id=published.external_id,
                draft_url=published.url,
            )
            logger.info(
                f"✓ Issue is in {published.publisher} ({published.status}): "
                f"id {published.external_id}"
                + (f", {published.url}" if published.url else "")
            )
            # Where readers see it: a draft is only reachable on the CMS; a public post
            # may live on another host (NEWSLETTER_PUBLIC_URL_TEMPLATE).
            reader_url = (
                public_url(
                    self.settings.newsletter_public_url_template,
                    published.slug,
                    published.url,
                )
                if published.status == "publish"
                else published.url
            )
            if reader_url != published.url:
                logger.info(f"  readers' URL: {reader_url}")
            results["public_url"] = reader_url

            # Email the same issue to subscribers. The run is already a success —
            # the issue exists on the web and must never ship twice — so a mail
            # failure is recorded on the run and reported, not raised.
            if self.newsletter_mailer is not None:
                mailer = self.newsletter_mailer
                deliver = self.settings.newsletter_delivery == "publish"
                logger.info(
                    f"\n{'Sending' if deliver else 'Drafting'} the email via {mailer.name}"
                )
                logger.info("-" * 60)
                try:
                    email_html = self.renderer.render_email(
                        issue,
                        html,
                        web_url=reader_url,
                        unsubscribe_url=mailer.unsubscribe_placeholder,
                    )
                    mailed = await mailer.send(issue, email_html, deliver=deliver)
                    run.mail_id = mailed.external_id
                    run.mail_status = mailed.status
                    results["mail"] = {
                        "mailer": mailed.mailer,
                        "external_id": mailed.external_id,
                        "status": mailed.status,
                        "url": mailed.url,
                    }
                    logger.info(
                        f"✓ Email {mailed.status} via {mailed.mailer}: id {mailed.external_id}"
                    )
                except Exception as e:  # noqa: BLE001 - recorded, not re-raised
                    run.mail_error = str(e)[:500]
                    results["errors"].append(f"email ({mailer.name}): {e}")
                    logger.error(f"✗ Email failed: {e}")

            # Share the issue's link. Only a public post is worth sharing: a draft
            # URL 404s for readers. Never fatal; reported like a mail failure.
            if self.social_media_agent is not None:
                wait = self.settings.newsletter_public_url_wait_seconds
                live = True
                if published.status == "publish" and reader_url and wait > 0:
                    logger.info(
                        f"Waiting up to {wait}s for {reader_url} to answer 200 before sharing"
                    )
                    live = await wait_until_live(reader_url, timeout=wait)
                    if not live:
                        results["errors"].append(
                            f"social: {reader_url} did not answer 200 within {wait}s; not shared"
                        )
                        logger.error(results["errors"][-1])
                if published.status == "publish" and reader_url and live:
                    copy = social_copy(issue, self.settings.newsletter_social_label)
                    logger.info("\nPosting the issue to social media")
                    logger.info("-" * 60)
                    try:
                        social = await self.social_media_agent.post_link(
                            url=reader_url,
                            title=copy.title,
                            linkedin_text=copy.linkedin,
                            tweet_text=copy.tweet,
                            description=copy.description,
                        )
                        results["social"] = social
                        for platform, r in social.items():
                            if r.get("failed"):
                                results["errors"].append(
                                    f"social ({platform}): {r.get('error')}"
                                )
                    except Exception as e:  # noqa: BLE001
                        results["errors"].append(f"social: {e}")
                        logger.error(f"✗ Social posting failed: {e}")
                elif published.status != "publish":
                    logger.info(
                        f"Social posting skipped: post is '{published.status}', not public"
                    )

            self.run_log.record(run)
            self.pipeline_store.record_newsletter_run(run.to_dict())
            results["status"] = "published"
            results["publisher"] = published.publisher
            results["external_id"] = published.external_id
            results["draft_url"] = published.url
            logger.info(f"Watermark advanced to {window_end.isoformat()}")
            return results

        except Exception as e:
            logger.error(f"Newsletter run failed: {e}", exc_info=True)
            results["status"] = "failed"
            results["errors"].append(str(e))
            raise

    async def run_pipeline(self, skip_publishing: bool = False) -> dict:
        """
        Run the complete WAYR pipeline.

        Args:
            skip_publishing: If True, generate articles but don't publish

        Returns:
            Dictionary with pipeline results and statistics
        """
        logger.info("=" * 60)
        logger.info("Starting WAYR Pipeline")
        logger.info("=" * 60)

        results: Dict[str, Any] = {
            "discovered": [],
            "classified": [],
            "generated": [],
            "edited": [],
            "published": [],
            "errors": [],
            "classification_errors": [],  # Track classification failures
        }

        try:
            # Determine number of steps based on which stages are enabled
            total_steps = sum(
                [
                    self.settings.enable_discovery,
                    self.settings.enable_classification,
                    self.settings.enable_author,
                    self.settings.enable_proofreading
                    and self.proofreader_agent is not None,
                    self.settings.enable_summary,
                    self.importance_agent is not None,  # dual mode scores the pool
                    True,  # publishing step is always logged (real or skipped)
                ]
            )
            step = 0

            # Step 0: Check for pending candidates from previous runs
            cached_candidates = self.article_tracker.get_news_candidates(
                max_age_hours=self.settings.max_candidate_age_hours
            )
            recovered_articles = []
            remaining_slots = self.settings.max_articles_published_per_run

            if cached_candidates:
                logger.info(
                    f"\n[Pre-step] Recovering {len(cached_candidates)} candidates from previous runs"
                )
                logger.info("-" * 60)
                recovered_articles = await self._reconstruct_candidates(
                    cached_candidates
                )
                if recovered_articles:
                    logger.info(
                        f"✓ Recovered {len(recovered_articles)} candidates from cache"
                    )
                    remaining_slots -= len(recovered_articles)

            # Skip discovery when disabled or when we already have enough candidates
            # The slot gate is per-story backpressure. In dual mode the same run also
            # feeds the newsletter pool, which must see everything — never gate it.
            skip_discovery = not self.settings.enable_discovery or (
                self.settings.pipeline_mode != "both" and remaining_slots <= 0
            )

            if skip_discovery:
                if not self.settings.enable_discovery:
                    logger.info(
                        "Discovery disabled (ENABLE_DISCOVERY=false), using cached candidates only"
                    )
                else:
                    logger.info(
                        f"Have {len(recovered_articles)} candidates (>= {self.settings.max_articles_published_per_run} limit), skipping discovery"
                    )
                results["discovered"] = []
                results["classified"] = []
                results["classification_errors"] = []
                classified = recovered_articles

                if not classified:
                    logger.warning(
                        "No cached candidates available and discovery skipped. Pipeline stopped."
                    )
                    return results
            else:
                # Steps 1–2: the shared research trunk (discovery + classification)
                research, step = await self._research(step, total_steps)
                discovered = research.discovered
                results["discovered"] = discovered

                # Dual mode: score the day's news into the pool for the newsletter
                # compile, then continue into the per-story branch as before.
                if self.importance_agent is not None:
                    results.setdefault("scored", [])
                    results.setdefault("importance_errors", [])
                    step = await self._importance_step(
                        research.news, results, step, total_steps
                    )

                if not discovered and not recovered_articles:
                    logger.warning(
                        "No new articles discovered and no cached candidates. Pipeline stopped."
                    )
                    return results
                if not discovered:
                    logger.info("Proceeding with cached candidates only")

                classified = research.classified
                results["classified"] = classified
                results["classification_errors"] = research.classification_errors

                # Filter: Keep only NEWS content type
                news_articles = research.news

                # Log filtering results
                non_news_count = len(classified) - len(news_articles)
                if non_news_count > 0:
                    logger.info(f"Filtered out {non_news_count} non-news articles:")
                    for c in classified:
                        if c.content_type != ContentType.NEWS:
                            logger.info(
                                f"  - [{c.content_type.value}] '{c.original.title}' ({c.original.source})"
                            )

                # Merge: recovered candidates first (FIFO priority), then newly classified
                classified = recovered_articles + news_articles

                if not classified:
                    logger.warning(
                        "No news articles found or met confidence threshold. Pipeline stopped."
                    )
                    return results

            logger.info(f"✓ Total {len(classified)} news articles ready for generation")
            if recovered_articles:
                logger.info(f"  - {len(recovered_articles)} from cache (previous runs)")
                logger.info(
                    f"  - {len(classified) - len(recovered_articles)} newly classified"
                )

            # Log category distribution
            category_counts: dict = {}
            for c in classified:
                key = c.category or "Uncategorized"
                category_counts[key] = category_counts.get(key, 0) + 1
            for category, count in category_counts.items():
                logger.info(f"  - {category}: {count}")

            # Stop here if the author stage is disabled — nothing downstream
            # (proofreading, summaries, publishing) can run without generated content.
            if not self.settings.enable_author or not self.author_agent:
                logger.info(
                    "\nAuthor disabled (ENABLE_AUTHOR=false). Pipeline stopped after classification."
                )
                return results

            # Limit to max_articles_published_per_run BEFORE generation (no wasteful generation)
            articles_to_generate = classified
            if len(classified) > self.settings.max_articles_published_per_run:
                logger.info(
                    f"Limiting from {len(classified)} classified articles to "
                    f"{self.settings.max_articles_published_per_run} for generation"
                )
                articles_to_generate = classified[
                    : self.settings.max_articles_published_per_run
                ]

            # Step 3: Generate articles
            step += 1
            logger.info(
                f"\n[Step {step}/{total_steps}] Article Generation ({self.settings.model_author})"
            )
            logger.info("-" * 60)
            generated = await self.author_agent.generate_articles(articles_to_generate)
            results["generated"] = generated

            if not generated:
                logger.warning("No articles generated. Pipeline stopped.")
                return results

            self.pipeline_store.record_generated(generated)
            logger.info(f"✓ Generated {len(generated)} articles")

            # Step 4: Proofread articles (if enabled)
            if self.settings.enable_proofreading and self.proofreader_agent:
                step += 1
                logger.info(
                    f"\n[Step {step}/{total_steps}] Proofreading & Editing ({self.settings.model_proofreader})"
                )
                logger.info("-" * 60)
                edited = await self.proofreader_agent.proofread_articles(generated)
                results["edited"] = edited

                if not edited:
                    logger.warning("No articles edited. Pipeline stopped.")
                    return results

                logger.info(f"✓ Proofread {len(edited)} articles")

                # Log some edit summaries
                for art in edited[:3]:  # Show first 3
                    logger.info(f"  - {art.edited_title}")
                    logger.debug(f"    Changes: {art.edit_summary}")
            else:
                # If proofreading disabled, create pass-through EditedArticle objects
                logger.info("\nProofreading disabled, skipping...")
                edited = []
                for gen in generated:
                    from wayr.models import EditedArticle

                    edited.append(
                        EditedArticle(
                            generated=gen,
                            edited_title=gen.generated_title,
                            edited_content=gen.generated_content,
                            edit_summary="Proofreading disabled",
                            editor_model="none",
                        )
                    )
                results["edited"] = edited

            # Step 5: Generate summaries (for WordPress excerpt + social media)
            if self.settings.enable_summary and self.summary_agent:
                step += 1
                logger.info(f"\n[Step {step}/{total_steps}] Generating Summaries")
                logger.info("-" * 60)
                edited = await self.summary_agent.generate_summaries(edited)
                logger.info(f"✓ Generated summaries for {len(edited)} articles")
            else:
                logger.info(
                    "\nSummary disabled (ENABLE_SUMMARY=false), excerpt will fall back to content extraction"
                )

            # Persist edited + summary output together so summary_output in
            # Supabase includes the social/SEO summary.
            if edited:
                self.pipeline_store.record_edited(edited)

            # Step 6: Publish articles
            step += 1
            if (
                not skip_publishing
                and self.settings.enable_publishing
                and self.publishing_agent is not None
            ):
                logger.info(f"\n[Step {step}/{total_steps}] Publishing to WordPress")
                logger.info("-" * 60)

                # All articles were already limited before generation
                articles_to_publish = edited

                published = await self.publishing_agent.publish_articles(
                    articles_to_publish
                )
                results["published"] = published

                if not published:
                    logger.warning("No articles published.")
                else:
                    logger.info(f"✓ Published {len(published)} articles")

                    # Mark published articles as processed
                    # Extract original RSS articles from the published articles
                    published_rss_articles = [
                        pub.edited.generated.classified.original for pub in published
                    ]
                    self.article_tracker.record_published(published_rss_articles)
                    self.pipeline_store.record_published(published)

                    # Log published URLs
                    logger.info("\nPublished Articles:")
                    for pub in published:
                        logger.info(f"  - {pub.edited.edited_title}")
                        logger.info(f"    {pub.wordpress_url}")

                    # Step 7: Post to Social Media (Twitter, LinkedIn, etc.)
                    if self.social_media_agent:
                        step += 1
                        logger.info(f"\n[Step {step}] Posting to Social Media")
                        logger.info("-" * 60)

                        social_results = (
                            await self.social_media_agent.post_to_social_media(
                                published
                            )
                        )
                        results["social_media"] = social_results

                        # Log social media results
                        if social_results.get("twitter"):
                            twitter = social_results["twitter"]
                            logger.info(
                                f"✓ Twitter: {twitter['posted']} posted, {twitter['failed']} failed, {twitter['skipped']} skipped"
                            )
                        if social_results.get("linkedin"):
                            linkedin = social_results["linkedin"]
                            logger.info(
                                f"✓ LinkedIn: {linkedin['posted']} posted, {linkedin['failed']} failed, {linkedin['skipped']} skipped"
                            )
                    else:
                        logger.info(
                            "\nSocial Media posting disabled (toggle off or no credentials)"
                        )

            else:
                skip_reason = (
                    "preview mode" if skip_publishing else "ENABLE_PUBLISHING=false"
                )
                logger.info(
                    f"\n[Step {step}/{total_steps}] Publishing skipped ({skip_reason})"
                )
                logger.info("-" * 60)

                # All articles were already limited before generation
                articles_to_show = edited

                logger.info("Edited articles (not published):")
                for art in articles_to_show:
                    logger.info(f"  - {art.edited_title}")
                    logger.info(
                        f"    Category: {art.generated.classified.category or 'Uncategorized'}"
                    )

            # Pipeline complete
            logger.info("\n" + "=" * 60)
            logger.info("Pipeline Complete!")
            logger.info("=" * 60)
            logger.info(f"Discovered: {len(results['discovered'])}")
            logger.info(f"Filtered (news only): {len(results.get('filtered', []))}")
            logger.info(f"Classified: {len(results['classified'])}")
            logger.info(f"Generated: {len(results['generated'])}")
            logger.info(f"Edited: {len(results['edited'])}")
            logger.info(f"Published: {len(results['published'])}")
            if results.get("social_media"):
                social = results["social_media"]
                if social.get("twitter"):
                    twitter = social["twitter"]
                    logger.info(
                        f"Posted to Twitter: {twitter['posted']} (failed: {twitter['failed']}, skipped: {twitter['skipped']})"
                    )
                if social.get("linkedin"):
                    linkedin = social["linkedin"]
                    logger.info(
                        f"Posted to LinkedIn: {linkedin['posted']} (failed: {linkedin['failed']}, skipped: {linkedin['skipped']})"
                    )
            if results["classification_errors"]:
                logger.warning(
                    f"Classification errors: {len(results['classification_errors'])}"
                )
            logger.info("=" * 60)

            # Cost summary — collect usage from all enabled LLM-backed agents
            agents_with_usage = [
                agent
                for agent in (
                    self.dedup_agent,
                    self.industry_classifier_agent,
                    self.news_classifier_agent,
                    self.category_classifier_agent,
                    self.author_agent,
                    self.proofreader_agent,
                    self.summary_agent,
                )
                if agent is not None
            ]

            cost_summaries = [
                a.get_usage_summary()
                for a in agents_with_usage
                if hasattr(a, "get_usage_summary")
            ]
            total_input = sum(s["input_tokens"] for s in cost_summaries)
            total_output = sum(s["output_tokens"] for s in cost_summaries)
            total_calls = sum(s["calls"] for s in cost_summaries)
            total_cost = sum(s["estimated_cost_usd"] for s in cost_summaries)
            num_articles = len(results.get("published", results.get("edited", [])))

            logger.info("\n=== LLM Cost Summary ===")
            for s in cost_summaries:
                if s["calls"] > 0:
                    logger.info(
                        f"  {s['agent']:<25} ({s['provider']}:{s['model']})  "
                        f"{s['calls']} calls  {s['input_tokens']:,} in / {s['output_tokens']:,} out  "
                        f"~${s['estimated_cost_usd']:.4f}"
                    )
            logger.info(f"  {'─' * 70}")
            logger.info(
                f"  TOTAL: {total_calls} calls, {total_input:,} in / {total_output:,} out, "
                f"~${total_cost:.4f}"
            )
            if num_articles > 0:
                logger.info(f"  Per article: ~${total_cost / num_articles:.4f}")
            logger.info("=" * 60)

            results["cost_summary"] = {
                "agents": cost_summaries,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "total_calls": total_calls,
                "total_estimated_cost_usd": round(total_cost, 6),
                "per_article_cost_usd": (
                    round(total_cost / num_articles, 6) if num_articles > 0 else 0
                ),
            }

            return results

        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)
            results["errors"].append(str(e))
            raise

    async def test_connections(self) -> bool:
        """
        Test all external connections (WordPress, OpenAI, Anthropic).

        Returns:
            True if all connections successful
        """
        logger.info("Testing connections...")

        # Test the newsletter publisher (newsletter mode only)
        pub_ok = True
        if self.newsletter_publisher is not None:
            logger.info(f"Testing {self.newsletter_publisher.name} connection...")
            pub_ok = await self.newsletter_publisher.test_connection()
            if pub_ok:
                logger.info(f"✓ {self.newsletter_publisher.name} connection successful")
            else:
                logger.error(f"✗ {self.newsletter_publisher.name} connection failed")

        # Test the mailer (newsletter mode with NEWSLETTER_MAILER set)
        if self.newsletter_mailer is not None:
            logger.info(f"Testing {self.newsletter_mailer.name} connection...")
            mail_ok = await self.newsletter_mailer.test_connection()
            if mail_ok:
                logger.info(f"✓ {self.newsletter_mailer.name} connection successful")
            else:
                logger.error(f"✗ {self.newsletter_mailer.name} connection failed")
            pub_ok = pub_ok and mail_ok

        # Test WordPress (news mode only)
        if self.publishing_agent is None:
            logger.info(
                "WordPress publishing agent: not built in newsletter mode, skipped"
            )
            wp_ok = pub_ok
        else:
            logger.info("Testing WordPress connection...")
            wp_ok = self.publishing_agent.test_connection()
            if wp_ok:
                logger.info("✓ WordPress connection successful")
            else:
                logger.error("✗ WordPress connection failed")

        # Test OpenAI: the key, and that every OpenAI model this deployment names
        # exists. A read (models.retrieve) rather than a completion, so reasoning
        # models with their own parameter rules are not an obstacle.
        logger.info("Testing OpenAI connection...")
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=self.settings.openai_api_key)
            names = sorted(
                {
                    cfg["model_name"]
                    for cfg in self._configured_models()
                    if cfg["provider"] == "openai"
                }
            )
            if names:
                for name in names:
                    await client.models.retrieve(name)
                logger.info(f"✓ OpenAI connection successful ({', '.join(names)})")
            else:
                await client.models.list()
                logger.info(
                    "✓ OpenAI connection successful (no OpenAI model configured)"
                )
            openai_ok = True
        except Exception as e:
            logger.error(f"✗ OpenAI connection failed: {e}")
            openai_ok = False

        # Test Anthropic (if proofreading enabled)
        anthropic_ok = True
        if self.settings.enable_proofreading:
            logger.info("Testing Anthropic connection...")
            try:
                from anthropic import AsyncAnthropic

                anthropic_client = AsyncAnthropic(
                    api_key=self.settings.anthropic_api_key
                )
                await anthropic_client.messages.create(
                    model=self.settings.anthropic_model,
                    max_tokens=10,
                    messages=[{"role": "user", "content": "Hello"}],
                )
                logger.info("✓ Anthropic connection successful")
                anthropic_ok = True
            except Exception as e:
                logger.error(f"✗ Anthropic connection failed: {e}")
                anthropic_ok = False
        else:
            logger.info("⊘ Anthropic test skipped (proofreading disabled)")

        return wp_ok and openai_ok and anthropic_ok

    async def preview_pipeline(self) -> dict:
        """
        Run pipeline in preview mode (no publishing).

        Returns:
            Pipeline results
        """
        return await self.run_pipeline(skip_publishing=True)


async def main():
    """Main entry point for running the orchestrator."""
    orchestrator = WAYROrchestrator()

    # Test connections first
    connections_ok = await orchestrator.test_connections()

    if not connections_ok:
        logger.error("Connection tests failed. Please check configuration.")
        return

    # Run the pipeline
    await orchestrator.run_pipeline()


if __name__ == "__main__":
    asyncio.run(main())
