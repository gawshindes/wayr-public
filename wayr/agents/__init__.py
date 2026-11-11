"""AI agents for WAYR platform."""

from wayr.agents.discovery import NewsDiscoveryAgent
from wayr.agents.industry_classifier import IndustryClassifierAgent
from wayr.agents.news_classifier import NewsClassifierAgent
from wayr.agents.category_classifier import CategoryClassifierAgent
from wayr.agents.author import AuthorAgent
from wayr.agents.proofreader import ProofreaderAgent
from wayr.agents.publishing import PublishingAgent
from wayr.agents.social_media import SocialMediaAgent
from wayr.agents.deduplication import DeduplicationAgent
from wayr.agents.summary import SummaryAgent
from wayr.agents.importance import ImportanceAgent
from wayr.agents.newsletter import NewsletterAgent

__all__ = [
    "NewsDiscoveryAgent",
    "IndustryClassifierAgent",
    "NewsClassifierAgent",
    "CategoryClassifierAgent",
    "DeduplicationAgent",
    "AuthorAgent",
    "ProofreaderAgent",
    "SummaryAgent",
    "ImportanceAgent",
    "NewsletterAgent",
    "PublishingAgent",
    "SocialMediaAgent",
]
