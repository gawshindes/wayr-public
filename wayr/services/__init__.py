"""Service layer for WAYR platform."""

from wayr.services.rss_fetcher import RSSFetcher
from wayr.services.content_extractor import ContentExtractor
from wayr.services.wordpress_client import WordPressClient
from wayr.services.article_tracker import ArticleTracker
from wayr.services.supabase_store import SupabasePipelineStore
from wayr.services.feed_ledger import FeedLedger
from wayr.services.newsletter_runs import (
    NewsletterRun,
    NewsletterRunLog,
    compute_window,
)
from wayr.services.newsletter_selection import Selection, select_stories
from wayr.services.newsletter_assign import Assignment, assign_stories
from wayr.services.newsletter_renderer import NewsletterRenderer

__all__ = [
    "RSSFetcher",
    "ContentExtractor",
    "WordPressClient",
    "ArticleTracker",
    "SupabasePipelineStore",
    "FeedLedger",
    "NewsletterRun",
    "NewsletterRunLog",
    "compute_window",
    "Selection",
    "select_stories",
    "Assignment",
    "assign_stories",
    "NewsletterRenderer",
]
