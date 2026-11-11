"""Newsletter publishers: where a compiled issue goes.

One active publisher per deployment, selected by NEWSLETTER_PUBLISHER and
resolved through a name → class registry, so adding a target is one adapter
file plus one registry line. See docs/multi-publisher-plan.md for the
per-story (news mode) version of the same seam.
"""

from typing import Dict, Type

from wayr.publishers.base import NewsletterPublisher, PublishError, PublishResult
from wayr.publishers.beehiiv import BeehiivPublisher
from wayr.publishers.wordpress import WordPressNewsletterPublisher

REGISTRY: Dict[str, Type[NewsletterPublisher]] = {
    BeehiivPublisher.name: BeehiivPublisher,
    WordPressNewsletterPublisher.name: WordPressNewsletterPublisher,
}


def get_newsletter_publisher(settings) -> NewsletterPublisher:
    """Build the configured publisher. Unknown names fail at startup, not at send time."""
    name = (settings.newsletter_publisher or "").strip().lower()
    if name not in REGISTRY:
        raise ValueError(
            f"Unknown NEWSLETTER_PUBLISHER '{name}'. Available: {sorted(REGISTRY)}"
        )
    return REGISTRY[name].from_settings(settings)


__all__ = [
    "NewsletterPublisher",
    "PublishError",
    "PublishResult",
    "BeehiivPublisher",
    "WordPressNewsletterPublisher",
    "REGISTRY",
    "get_newsletter_publisher",
]
