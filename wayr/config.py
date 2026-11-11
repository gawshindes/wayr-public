"""Configuration management for WAYR platform."""

from typing import List, Literal, Optional, Union
from pydantic import (
    AliasChoices,
    Field,
    HttpUrl,
    PrivateAttr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from wayr.models.sources import Source, parse_sources_text, sources_from_urls


# Which agents each pipeline mode is allowed to build. An agent is constructed
# iff it is in the mode's set AND its ENABLE_* flag (where one exists) is on.
# Execution order stays in the orchestrator's code, fully typed; only membership
# is declared here. Adding a mode or moving an agent between modes is one line,
# and the orchestrator never mentions a mode by name.
#
# "newsletter_publisher" is resolved through wayr.publishers.REGISTRY by
# NEWSLETTER_PUBLISHER (beehiiv, wordpress, ...).
MODE_AGENTS: dict[str, frozenset[str]] = {
    "news": frozenset(
        {
            "discovery",
            "dedup",
            "classification",
            "author",
            "proofreader",
            "summary",
            "publishing",
            "social",
        }
    ),
    "newsletter": frozenset(
        {
            "discovery",
            "dedup",
            "classification",
            "importance",
            "summary",
            "newsletter",
            "newsletter_publisher",  # whichever NEWSLETTER_PUBLISHER names
            "newsletter_mailer",  # whichever NEWSLETTER_MAILER names (none → not built)
            "social",  # the published issue's link, once a day (publish delivery only)
        }
    ),
}
# Dual mode: a news site that also sends a newsletter. One intraday job researches
# once, scores importance into the pool, then continues into the per-story
# branch; the compile job reads the pool at its own cron. Union of the two sets.
MODE_AGENTS["both"] = MODE_AGENTS["news"] | MODE_AGENTS["newsletter"]


def mode_schedules(
    mode: str,
    *,
    pipeline_cron: str = "0 */2 * * *",
    prework_cron: str = "0 */2 * * *",
    newsletter_cron: str = "30 6 * * *",
) -> dict[str, Optional[str]]:
    """Which Modal functions a deployment schedules, by mode.

    Returns {"pipeline": cron|None, "prework": cron|None, "newsletter": cron|None}.
    news schedules only the per-story pipeline; newsletter schedules prework and
    the compile job and leaves the pipeline unscheduled (still callable by hand).
    The newsletter cron should sit at :30 so it never coincides with the
    on-the-hour prework tick on the shared volume.
    """
    if mode not in MODE_AGENTS:
        raise ValueError(
            f"Unknown pipeline mode '{mode}'. Available: {sorted(MODE_AGENTS)}"
        )
    if mode == "newsletter":
        return {
            "pipeline": None,
            "prework": prework_cron,
            "newsletter": newsletter_cron,
        }
    if mode == "both":
        # the pipeline does prework's job (research + importance) on its own tick
        return {
            "pipeline": pipeline_cron,
            "prework": None,
            "newsletter": newsletter_cron,
        }
    return {"pipeline": pipeline_cron, "prework": None, "newsletter": None}


# ENABLE_* flag → agent name in MODE_AGENTS (only agents that have a flag).
_ENABLE_FLAGS: dict[str, str] = {
    "enable_discovery": "discovery",
    "enable_classification": "classification",
    "enable_author": "author",
    "enable_proofreading": "proofreader",
    "enable_summary": "summary",
    "enable_publishing": "publishing",
    "enable_social_posting": "social",
}


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # OpenAI Configuration (for Author Agent)
    openai_api_key: str = Field(
        ..., description="OpenAI API key for content generation"
    )
    openai_model: str = Field(
        default="gpt-4", description="OpenAI model to use (legacy, use model_* instead)"
    )
    openai_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    openai_max_tokens: int = Field(default=800, ge=100, le=4000)

    # Anthropic Configuration (for Proofreader Agent)
    anthropic_api_key: Optional[str] = Field(
        default=None, description="Anthropic API key for proofreading"
    )
    anthropic_model: str = Field(
        default="claude-3-5-sonnet-20241022",
        description="Anthropic Claude model to use for proofreading (legacy, use model_* instead)",
    )
    anthropic_max_tokens: int = Field(default=2000, ge=100, le=4000)

    # Google AI Configuration (for Gemini models)
    google_api_key: Optional[str] = Field(
        default=None, description="Google AI API key for Gemini models"
    )

    # Supabase (used by offline scripts — test case builders, backfills)
    supabase_url: Optional[str] = Field(
        default=None, description="Supabase project URL (scripts-only)"
    )
    # The server-side key: an `sb_secret_...` key from Project Settings → API Keys.
    # The legacy name SUPABASE_SERVICE_ROLE_KEY (the old service_role JWT) is still
    # read, so an existing env file keeps working.
    supabase_secret_key: Optional[str] = Field(  # type: ignore[pydantic-alias]
        default=None,
        validation_alias=AliasChoices(
            "SUPABASE_SECRET_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
            "supabase_secret_key",
            "supabase_service_role_key",
        ),
        description="Supabase secret key (bypasses RLS; server-side only). Legacy: SUPABASE_SERVICE_ROLE_KEY",
    )
    region: Optional[str] = Field(
        default=None,
        description=(
            "Pipeline region tag written to pipeline_articles.region at "
            "discovery time. Use 'us' or 'india' in prod, 'staging' locally."
        ),
    )

    # Twitter API Configuration (for Social Media Agent)
    twitter_api_key: Optional[str] = Field(
        default=None, description="Twitter API key (Consumer Key)"
    )
    twitter_api_secret: Optional[str] = Field(
        default=None, description="Twitter API secret (Consumer Secret)"
    )
    twitter_access_token: Optional[str] = Field(
        default=None, description="Twitter access token"
    )
    twitter_access_token_secret: Optional[str] = Field(
        default=None, description="Twitter access token secret"
    )

    # LinkedIn API Configuration (Community Management API - for posting to Company Page)
    linkedin_access_token: Optional[str] = Field(
        default=None, description="LinkedIn OAuth access token"
    )
    linkedin_refresh_token: Optional[str] = Field(
        default=None, description="LinkedIn OAuth refresh token (for auto-renewal)"
    )
    linkedin_client_id: Optional[str] = Field(
        default=None, description="LinkedIn app client ID"
    )
    linkedin_client_secret: Optional[str] = Field(
        default=None, description="LinkedIn app client secret"
    )
    linkedin_organization_urn: Optional[str] = Field(
        default=None,
        description="LinkedIn Organization URN (urn:li:organization:YOUR_ORG_ID)",
    )

    # Model Selection Per Agent (format: provider:model_name)
    model_classification: str = Field(
        default="openai:gpt-3.5-turbo",
        description="Fallback model for all classification agents when per-agent vars are unset",
    )
    model_classification_industry: Optional[str] = Field(
        default=None,
        description="Override model for the industry classifier. Falls back to model_classification.",
    )
    model_classification_news: Optional[str] = Field(
        default=None,
        description="Override model for the news classifier. Falls back to model_classification.",
    )
    model_classification_category: Optional[str] = Field(
        default=None,
        description="Override model for the category classifier. Falls back to model_classification.",
    )
    model_author: str = Field(
        default="openai:gpt-3.5-turbo",
        description="Model for Author Agent (format: provider:model_name)",
    )
    model_proofreader: str = Field(
        default="anthropic:claude-3-haiku-20240307",
        description="Model for Proofreader Agent (format: provider:model_name)",
    )
    model_summary: str = Field(
        default="google:gemini-flash-latest",
        description="Model for Social Media summary generation (format: provider:model_name)",
    )
    model_dedup: str = Field(
        default="google:gemini-flash-latest",
        description="Model for article deduplication (format: provider:model_name)",
    )
    model_importance: str = Field(
        default="google:gemini-flash-latest",
        description="Model for the ImportanceAgent, newsletter mode (format: provider:model_name)",
    )
    model_newsletter: str = Field(
        default="google:gemini-flash-latest",
        description="Model for the NewsletterAgent compile call (format: provider:model_name)",
    )
    newsletter_temperature: float = Field(
        default=0.7, ge=0.0, le=2.0, description="Temperature for the compile call"
    )
    newsletter_max_tokens: int = Field(
        default=4000, ge=500, le=8000, description="Max tokens for the compile call"
    )
    newsletter_importance_criteria: Optional[str] = Field(
        default=None,
        description=(
            "Who the newsletter's readers are and what matters to them. Fills the "
            "criteria block of the importance prompt. Unset → a default derived from "
            "INDUSTRY. This is where a customer's onboarding description lands."
        ),
    )
    newsletter_importance_criteria_file: Optional[str] = Field(
        default=None,
        description=(
            "Path to a markdown file with the reader criteria. Used when the inline "
            "var is unset. Start from wayr/templates/importance_criteria.example.md."
        ),
    )

    # Newsletter selection — thresholds, with fixed counts only as tail guardrails.
    # The importance prompt's 0-10 anchors line up: 6+ ≈ feature, 4-5 ≈ roundup.
    newsletter_voice: Optional[str] = Field(
        default=None,
        description=(
            "Inline voice guide for the compile call: tone, how the opening "
            "works, how long commentary runs. Overrides NEWSLETTER_VOICE_FILE. "
            "Unset → packaged default (plain house voice)."
        ),
    )
    newsletter_voice_file: Optional[str] = Field(
        default=None,
        description=(
            "Path to a markdown voice guide (see newsletters/ai-daily/voice.md). "
            "The per-customer seam for how the newsletter *sounds*, the way "
            "NEWSLETTER_IMPORTANCE_CRITERIA_FILE is the seam for what it *picks*."
        ),
    )
    newsletter_feature_threshold: float = Field(
        default=0.6, ge=0.0, le=1.0, description="Score at or above → full treatment"
    )
    newsletter_roundup_threshold: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Score at or above → bare roundup link",
    )
    newsletter_min_stories: int = Field(
        default=5,
        ge=1,
        description="Featured floor on a slow day (backfilled from roundup)",
    )
    newsletter_max_stories: int = Field(
        default=15, ge=1, description="Featured ceiling; overflow demotes into roundup"
    )
    newsletter_max_story_age_hours: int = Field(
        default=36,
        ge=1,
        description="Freshness by the source's PUBLISH date — distinct from window size",
    )

    # Newsletter run log
    # Newsletter structure — declarative so onboarding can generate both later
    newsletter_sections_file: Optional[str] = Field(
        default=None,
        description="YAML section schema; None → packaged generic default (wayr/templates/newsletter_sections_default.yaml)",
    )
    newsletter_template_file: Optional[str] = Field(
        default=None,
        description="Jinja2 HTML template; None → packaged default. A broken custom template falls back with an ERROR log.",
    )
    newsletter_runs_file: str = Field(
        default="data/newsletter_runs.json",
        description="Append-only run log; the watermark is derived from it (relative for local, /data/ for Modal)",
    )

    @model_validator(mode="after")
    def _check_newsletter_selection(self) -> "Settings":
        if self.newsletter_roundup_threshold >= self.newsletter_feature_threshold:
            raise ValueError(
                "NEWSLETTER_ROUNDUP_THRESHOLD must be below NEWSLETTER_FEATURE_THRESHOLD"
            )
        if self.newsletter_min_stories > self.newsletter_max_stories:
            raise ValueError(
                "NEWSLETTER_MIN_STORIES must not exceed NEWSLETTER_MAX_STORIES"
            )
        return self

    dedup_window_days: int = Field(
        default=7,
        description="Number of days to look back for cross-batch deduplication",
    )

    # Agent Temperature & Token Settings
    classification_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="Temperature for classification (0.3 recommended)",
    )
    classification_max_content_chars: Optional[int] = Field(
        default=None,
        ge=500,
        description=(
            "Cap on article text sent to the three classifiers (the stored "
            "article keeps its full content). Unset sends everything the "
            "extractor returned. Classification decisions come from the lede; "
            "the tail is tokens spent against a per-minute budget — 3000 "
            "halves the cost of a run on a rate-limited tier."
        ),
    )
    classification_max_tokens: int = Field(
        default=1024,
        ge=100,
        le=4000,
        description="Max tokens for classification response",
    )

    author_temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="Temperature for article generation (0.7 recommended)",
    )
    author_max_tokens: int = Field(
        default=2500, ge=100, le=4000, description="Max tokens for article generation"
    )

    proofreader_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="Temperature for proofreading (0.3 recommended)",
    )
    proofreader_max_tokens: int = Field(
        default=2000,
        ge=100,
        le=4000,
        description="Max tokens for proofreading response",
    )

    def get_model_config(self, model_spec: str) -> dict:
        """
        Parse model specification and return provider, model name, and API key.

        Args:
            model_spec: Format "provider:model_name" (e.g., "openai:gpt-4")
                       Also supports legacy format (just model name, assumes OpenAI)

        Returns:
            dict with keys: provider, model_name, api_key

        Raises:
            ValueError: If format is invalid or API key not configured

        Examples:
            >>> settings.get_model_config("openai:gpt-4")
            {"provider": "openai", "model_name": "gpt-4", "api_key": "sk-..."}

            >>> settings.get_model_config("google:gemini-2.0-flash-exp")
            {"provider": "google", "model_name": "gemini-2.0-flash-exp", "api_key": "AIza..."}
        """
        from wayr.utils import get_logger

        logger = get_logger(__name__)

        # Support legacy format (just model name, assume OpenAI)
        if ":" not in model_spec:
            logger.warning(
                f"Using legacy model format '{model_spec}'. "
                f"Please update to 'provider:model' format (e.g., 'openai:{model_spec}')"
            )
            return {
                "provider": "openai",
                "model_name": model_spec,
                "api_key": self.openai_api_key,
            }

        # Parse provider:model format
        parts = model_spec.split(":", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid model specification: '{model_spec}'. "
                f"Expected format: 'provider:model_name' (e.g., 'openai:gpt-4')"
            )

        provider, model_name = parts
        provider = provider.lower()

        # Map provider to API key
        api_key_map = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google": self.google_api_key,
        }

        if provider not in api_key_map:
            raise ValueError(
                f"Unknown provider '{provider}'. Supported: {', '.join(api_key_map.keys())}"
            )

        api_key = api_key_map[provider]

        # Validate API key exists
        if not api_key:
            raise ValueError(
                f"Model '{model_spec}' requires {provider.upper()}_API_KEY to be set in environment"
            )

        return {
            "provider": provider,
            "model_name": model_name,
            "api_key": api_key,
        }

    def get_classification_model(self, kind: str) -> dict:
        """Resolve a classification agent's model config with fallback.

        Args:
            kind: One of 'industry', 'news', 'category' (case-insensitive).

        Returns:
            Model config dict (same shape as get_model_config).
        """
        key = kind.lower().strip()
        overrides = {
            "industry": self.model_classification_industry,
            "news": self.model_classification_news,
            "category": self.model_classification_category,
        }
        if key not in overrides:
            raise ValueError(
                f"Unknown classification agent '{kind}'. "
                f"Expected one of industry, news, category."
            )
        spec = overrides[key] or self.model_classification
        return self.get_model_config(spec)

    def get_target_words(self) -> int:
        """Return target word count based on the configured article_type."""
        if self.article_type == "crisp":
            return self.article_target_words_crisp
        return self.article_target_words_detailed

    # Pipeline mode — selects which Modal functions are scheduled and which agents
    # the orchestrator builds. "news" is the existing per-story WordPress pipeline;
    # "newsletter" runs prework + a daily compile to a Beehiiv draft and never
    # touches WordPress, the author, the proofreader or social posting.
    pipeline_mode: Literal["news", "newsletter", "both"] = Field(
        default="news",
        description=(
            "news = per-story WordPress pipeline; newsletter = prework + daily Beehiiv draft; "
            "both = the news pipeline also scores the pool and a daily Beehiiv draft is compiled from it"
        ),
    )

    # WordPress Configuration — required in news mode only (see the validator below).
    wordpress_url: Optional[HttpUrl] = Field(
        default=None, description="WordPress site URL (e.g., https://wayr.today)"
    )
    wordpress_username: Optional[str] = Field(
        default=None, description="WordPress username for API"
    )
    wordpress_app_password: Optional[str] = Field(
        default=None, description="WordPress application password"
    )

    # Beehiiv Configuration — required in newsletter mode only.
    beehiiv_api_key: Optional[str] = Field(default=None, description="Beehiiv API key")
    beehiiv_publication_id: Optional[str] = Field(
        default=None, description="Beehiiv publication ID (pub_...)"
    )
    beehiiv_api_base: str = Field(
        default="https://api.beehiiv.com/v2",
        description="Beehiiv API base URL (override for a mock server)",
    )
    newsletter_publisher: str = Field(
        default="beehiiv",
        description="Active newsletter publisher, resolved via wayr.publishers.REGISTRY (beehiiv | wordpress)",
    )
    newsletter_wordpress_category: str = Field(
        default="Newsletter",
        description="WordPress category for newsletter drafts (wordpress publisher only)",
    )
    newsletter_public_url_template: Optional[str] = Field(
        default=None,
        description=(
            "Where readers see an issue when the site is not the CMS, with {slug}: e.g. "
            "https://wayrtoday.com/{slug}. Used for the email's read-on-the-web link and "
            "the social post once the post is public; otherwise the publisher's URL is used."
        ),
    )
    newsletter_public_url_wait_seconds: int = Field(
        default=0,
        description=(
            "Before sharing on social, wait up to this long for the readers' URL to answer "
            "200 (a front end with a cache may take minutes to show a new post; LinkedIn "
            "fetches the link when the post is created). 0 = share at once."
        ),
    )
    newsletter_social_label: str = Field(
        default="",
        description="Opens every social post so readers know it is an issue, not one story: e.g. DAILY DIGEST",
    )
    newsletter_wordpress_tags: str = Field(
        default="newsletter",
        description="Comma-separated WordPress tags on each issue post, so a front end can filter (wordpress publisher only)",
    )
    newsletter_delivery: Literal["draft", "publish"] = Field(
        default="draft",
        description=(
            "What the publisher (and mailer) do with a compiled issue: leave a draft "
            "for a human (default), or publish and send it. WordPress and Resend "
            "honour `publish`; Beehiiv's Send API is verified per plan and creates "
            "drafts until then."
        ),
    )

    # Mailer — how the published issue reaches subscribers (newsletter mode).
    # The publisher puts the issue on the web; the mailer sends the same HTML,
    # wrapped with a read-on-the-web link and the provider's unsubscribe link.
    newsletter_mailer: str = Field(
        default="none",
        description="Mailer resolved via wayr.mailers.REGISTRY: none (default) | resend | resend-direct",
    )
    resend_api_key: Optional[str] = Field(
        default=None, description="Resend API key (re_...)"
    )
    resend_segment_id: Optional[str] = Field(
        default=None, description="Resend Segment (contact list) the broadcast goes to"
    )
    resend_api_base: str = Field(
        default="https://api.resend.com",
        description="Resend API base URL (override for a mock server)",
    )
    newsletter_from: Optional[str] = Field(
        default=None,
        description="Sender for the email, e.g. 'WAYR <daily@wayrtoday.com>'; its domain must be verified at the mailer",
    )
    newsletter_reply_to: Optional[str] = Field(
        default=None, description="Reply-To address for the email (optional)"
    )
    # resend-direct: our own list (the site's `subscribers` table in Supabase).
    newsletter_unsubscribe_url: Optional[str] = Field(
        default=None,
        description=(
            "Per-subscriber unsubscribe link with {token}, e.g. "
            "https://example.com/newsletter/unsubscribe?token={token} (resend-direct)"
        ),
    )
    newsletter_test_recipients: str = Field(
        default="",
        description=(
            "Comma-separated addresses that receive the issue under NEWSLETTER_DELIVERY=draft "
            "(resend-direct): the operator's inbox, not the list"
        ),
    )

    @property
    def newsletter_test_recipient_list(self) -> list[str]:
        return [
            a.strip()
            for a in (self.newsletter_test_recipients or "").split(",")
            if a.strip()
        ]

    @model_validator(mode="after")
    def _require_publisher_credentials(self) -> "Settings":
        """Require the credentials of whichever publisher the mode actually uses.

        WordPress used to be unconditionally required, which made a newsletter-only
        deployment with no WordPress at all fail validation before anything ran.
        """
        required: tuple[tuple[str, object], ...]
        wordpress = (
            ("WORDPRESS_URL", self.wordpress_url),
            ("WORDPRESS_USERNAME", self.wordpress_username),
            ("WORDPRESS_APP_PASSWORD", self.wordpress_app_password),
        )
        beehiiv = (
            ("BEEHIIV_API_KEY", self.beehiiv_api_key),
            ("BEEHIIV_PUBLICATION_ID", self.beehiiv_publication_id),
        )
        publisher = (self.newsletter_publisher or "").strip().lower()
        by_publisher = {
            "beehiiv": ("Beehiiv", beehiiv),
            "wordpress": ("WordPress", wordpress),
        }
        pub_label, pub_creds = by_publisher.get(publisher, (publisher or "?", ()))

        if self.pipeline_mode == "news":
            required = wordpress
            label = "PIPELINE_MODE=news requires WordPress credentials"
        elif self.pipeline_mode == "newsletter":
            required = pub_creds
            label = f"PIPELINE_MODE=newsletter requires {pub_label} credentials"
        else:
            required = wordpress + tuple(c for c in pub_creds if c not in wordpress)
            label = f"PIPELINE_MODE=both requires WordPress and {pub_label} credentials"

        missing = [name for name, value in required if not value]
        if missing:
            raise ValueError(f"{label}; missing: {', '.join(missing)}")

        # The mailer is optional; when one is named, its credentials are not.
        mailer = (self.newsletter_mailer or "none").strip().lower()
        by_mailer = {
            "resend": (
                ("RESEND_API_KEY", self.resend_api_key),
                ("RESEND_SEGMENT_ID", self.resend_segment_id),
                ("NEWSLETTER_FROM", self.newsletter_from),
            ),
            "resend-direct": (
                ("RESEND_API_KEY", self.resend_api_key),
                ("NEWSLETTER_FROM", self.newsletter_from),
                ("NEWSLETTER_UNSUBSCRIBE_URL", self.newsletter_unsubscribe_url),
                ("SUPABASE_URL", self.supabase_url),
                ("SUPABASE_SECRET_KEY", self.supabase_secret_key),
            ),
        }
        if self.pipeline_mode != "news" and mailer in by_mailer:
            missing = [name for name, value in by_mailer[mailer] if not value]
            if missing:
                raise ValueError(
                    f"NEWSLETTER_MAILER={mailer} requires {', '.join(missing)}"
                )
        return self

    @property
    def mode_agents(self) -> frozenset[str]:
        """Agents this mode may build (see MODE_AGENTS)."""
        return MODE_AGENTS[self.pipeline_mode]

    @property
    def discovery_cap(self) -> Optional[int]:
        """Per-run discovery cap for this mode.

        The per-story pipeline caps discovery because authoring is expensive and
        the overflow is a durable backlog. The newsletter path wants to see
        everything and select later, so it is uncapped here and bounded only by
        ``discovery_bootstrap_max``. Mode logic lives in config so the
        orchestrator stays mode-agnostic.
        """
        if self.pipeline_mode == "newsletter":
            return None
        return self.max_articles_processed_per_run

    def ignored_enable_flags(self) -> List[str]:
        """ENABLE_* flags that are on but whose agent is not in this mode's set.

        Surfaced once at startup so an operator sees that, e.g., ENABLE_AUTHOR=true
        has no effect in newsletter mode rather than wondering why nothing ran.
        """
        return [
            field.upper()
            for field, agent in _ENABLE_FLAGS.items()
            if getattr(self, field) and agent not in self.mode_agents
        ]

    # RSS Feed Sources
    rss_feeds: Union[List[str], str] = Field(
        default=[
            "https://techcrunch.com/feed/",
            "https://www.theverge.com/rss/index.xml",
            "https://arstechnica.com/feed/",
            "https://www.wired.com/feed/rss",
            "https://venturebeat.com/feed/",
        ],
        description="List of RSS feed URLs to monitor",
    )

    rss_feeds_file: Optional[str] = Field(
        default=None,
        description=(
            "Path to a sources list, one source per line: a URL followed by optional "
            "`type=rss|page` (how to fetch; page is reserved and skipped) and "
            "`origin=press|brand` (whose words: a news site or the company itself) tokens, then an optional # comment. "
            "Blank lines are ignored; unknown tokens are errors; the same URL with "
            "different tokens is an error. When set it replaces RSS_FEEDS. The list "
            "is one of the artifacts onboarding emits, so it lives in a reviewable file."
        ),
    )

    # The typed view of the sources. `rss_feeds` stays the plain list of rss URLs
    # for every consumer that only fetches; this is what carries type and origin.
    _sources: List[Source] = PrivateAttr(default_factory=list)

    @property
    def sources(self) -> List[Source]:
        return list(self._sources)

    @model_validator(mode="after")
    def _load_rss_feeds_file(self) -> "Settings":
        if not self.rss_feeds_file:
            self._sources = sources_from_urls(self.rss_feeds)
            return self
        from pathlib import Path

        path = Path(self.rss_feeds_file)
        if not path.is_file():
            raise ValueError(f"RSS_FEEDS_FILE points at {path}, which does not exist")
        try:
            sources = parse_sources_text(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except ValueError as e:
            raise ValueError(f"RSS_FEEDS_FILE {e}") from None
        if not sources:
            raise ValueError(f"RSS_FEEDS_FILE {path} contains no feed URLs")
        rss = [src.url for src in sources if src.type == "rss"]
        if not rss:
            raise ValueError(
                f"RSS_FEEDS_FILE {path} has only type=page sources, which are not "
                "supported yet — nothing would be fetched"
            )
        self._sources = sources
        self.rss_feeds = rss
        return self

    @field_validator("rss_feeds", mode="before")
    @classmethod
    def parse_rss_feeds(cls, v):
        """Parse RSS feeds from comma-separated string or list."""
        if isinstance(v, str):
            # Split by comma and strip whitespace
            return [url.strip() for url in v.split(",") if url.strip()]
        return v

    # Agent Configuration
    fetch_interval_minutes: int = Field(
        default=30, ge=5, description="How often to fetch new articles"
    )
    max_articles_processed_per_run: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max articles to process through entire pipeline (discovery → generation)",
    )
    max_articles_published_per_run: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Max articles to actually publish (takes first N from processed batch)",
    )
    pipeline_concurrency: int = Field(
        default=10,
        ge=1,
        le=50,
        description=(
            "Max concurrent content extractions and classification chains. "
            "Bounds fan-out so a large discovery batch doesn't open hundreds "
            "of simultaneous HTTP requests or LLM calls."
        ),
    )
    min_content_words: int = Field(
        default=50,
        ge=0,
        description=(
            "Minimum word count of extracted article content required for an "
            "article to enter the processing pipeline. Articles below this "
            "threshold are dropped at discovery — we don't classify or "
            "generate off title-only input."
        ),
    )
    enable_proofreading: bool = Field(
        default=False,
        description="Enable proofreading with Claude (set to True to enable proofreading step)",
    )

    # Pipeline Stage Toggles (each step can be switched on/off via .env)
    enable_discovery: bool = Field(
        default=True,
        description="Run RSS discovery. When False, only cached candidates from previous runs are processed.",
    )
    enable_classification: bool = Field(
        default=True,
        description="Run industry/news/category classifiers. When False, all discovered articles are treated as news.",
    )
    enable_author: bool = Field(
        default=True,
        description="Generate AI articles. When False, the pipeline stops after classification.",
    )
    enable_summary: bool = Field(
        default=True,
        description="Generate summaries for WordPress excerpts and social media. When False, excerpt falls back to content extraction.",
    )
    enable_publishing: bool = Field(
        default=True,
        description="Publish to WordPress. When False, behaves like preview mode (articles generated but not published).",
    )
    enable_social_posting: bool = Field(
        default=True,
        description="Post published articles to social media (credentials still required).",
    )

    # Article Deduplication
    article_cache_file: str = Field(
        default="data/processed_articles.json",
        description="Path to file for tracking processed articles (relative for local, /data/ for Modal)",
    )
    feed_ledger_file: str = Field(
        default="data/feed_ledger.json",
        description="Every feed item ever seen + per-feed conditional-GET state (relative for local, /data/ for Modal)",
    )
    feed_ledger_retention_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="Drop ledger items not seen in any feed for this many days (keyed on last_seen_at, so a slow feed's window is never re-admitted)",
    )
    discovery_bootstrap_max: int = Field(
        default=300,
        ge=1,
        description="Hard per-run ceiling on admitted articles when discovery is uncapped (newsletter mode). Bounds a cold-ledger first run; steady state never reaches it.",
    )
    wp_taxonomy_cache_file: str = Field(
        default="data/wp_taxonomy.json",
        description="Path to persistent name→ID cache for WordPress categories/tags (relative for local, /data/ for Modal)",
    )
    article_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="How many days to remember processed articles (prevents re-processing)",
    )
    max_candidate_age_hours: int = Field(
        default=48,
        ge=1,
        le=168,
        description="Maximum age (hours) for classified-but-unpublished articles to be recoverable",
    )

    # Content Generation
    article_type: str = Field(
        default="crisp",
        description="Author article type: 'crisp' (~300 words) or 'detailed' (~800 words)",
    )
    article_target_words_crisp: int = Field(
        default=300, ge=100, le=3000, description="Target word count for crisp articles"
    )
    article_target_words_detailed: int = Field(
        default=800,
        ge=100,
        le=3000,
        description="Target word count for detailed articles",
    )
    add_source_citation: bool = Field(
        default=True,
        description="Append source citation link to generated article via Python post-processing",
    )

    @field_validator("article_type")
    @classmethod
    def _validate_article_type(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in {"crisp", "detailed"}:
            raise ValueError(f"article_type must be 'crisp' or 'detailed', got '{v}'")
        return v

    # Industry / Category Configuration
    industry: str = Field(
        description="Target industry for the publication. Required — set via INDUSTRY env.",
    )
    industry_categories: Union[List[str], str] = Field(
        description="Allowed news categories for the category classifier (primary/secondary). Comma-separated in env. Required — set via INDUSTRY_CATEGORIES env.",
    )

    @field_validator("industry_categories", mode="before")
    @classmethod
    def _parse_industry_categories(cls, v):
        if isinstance(v, str):
            return [c.strip() for c in v.split(",") if c.strip()]
        return v

    # Logging
    log_level: str = Field(
        default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR)"
    )

    # Platform Settings
    platform_name: str = Field(default="WAYR", description="Platform name")
    ai_disclosure_notice: str = Field(
        default="This article was generated by AI and cites original sources.",
        description="Notice to include in all AI-generated content",
    )


# Global settings instance
_settings: Optional[Settings] = None


def get_settings(env_file: Optional[str] = None) -> Settings:
    """Get or create the global settings instance.

    If ``env_file`` is provided, settings are (re)loaded from that path.
    Otherwise the cached instance is returned, or a fresh one is built
    using the default ``.env`` location.
    """
    global _settings
    if env_file is not None:
        _settings = Settings(_env_file=env_file)
        return _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset settings (mainly for testing)."""
    global _settings
    _settings = None
