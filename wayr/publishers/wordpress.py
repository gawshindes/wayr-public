"""WordPress draft publisher for newsletter issues.

The newsletter lands as a **draft post** in a configured category, using the
existing WordPressClient. Nothing is published; the editor sees it in the WP
admin. This is the zero-cost path for a site that already runs WordPress, and
the fallback when the Beehiiv plan does not include the Send API.
"""

import asyncio
import re
from typing import List, Optional

from wayr.models.newsletter import NewsletterIssue
from wayr.publishers.base import NewsletterPublisher, PublishError, PublishResult
from wayr.services.wordpress_client import WordPressClient
from wayr.utils import get_logger

logger = get_logger(__name__)

_H1 = re.compile(r"<h1\b[^>]*>.*?</h1>\s*", re.S | re.I)


class WordPressNewsletterPublisher(NewsletterPublisher):
    name = "wordpress"

    def __init__(
        self,
        site_url: str,
        username: str,
        app_password: str,
        category: str = "Newsletter",
        taxonomy_cache_file: str = "data/wp_taxonomy.json",
        client: Optional[WordPressClient] = None,
        delivery: str = "draft",
        tags: Optional[List[str]] = None,
    ):
        if not site_url or not username or not app_password:
            raise ValueError(
                "WordPressNewsletterPublisher needs site_url, username, app_password"
            )
        self.category = category
        self.tags = [x.strip() for x in (tags or []) if x and x.strip()]
        if delivery not in ("draft", "publish"):
            raise ValueError(f"delivery must be 'draft' or 'publish', not {delivery!r}")
        self.delivery = delivery
        self.client = client or WordPressClient(
            site_url=site_url,
            username=username,
            app_password=app_password,
            taxonomy_cache_file=taxonomy_cache_file,
        )

    @classmethod
    def from_settings(cls, settings) -> "WordPressNewsletterPublisher":
        return cls(
            site_url=str(settings.wordpress_url or ""),
            username=settings.wordpress_username or "",
            app_password=settings.wordpress_app_password or "",
            category=settings.newsletter_wordpress_category,
            taxonomy_cache_file=settings.wp_taxonomy_cache_file,
            delivery=getattr(settings, "newsletter_delivery", "draft"),
            tags=str(getattr(settings, "newsletter_wordpress_tags", "") or "").split(
                ","
            ),
        )

    async def publish(self, issue: NewsletterIssue, html: str) -> PublishResult:
        title = (issue.subject_line or issue.title).strip() or issue.title
        # WordPress renders the post title itself; the template's own <h1>
        # would show the heading twice.
        html = _H1.sub("", html, count=1)
        logger.info(
            f"WordPress: creating {self.delivery} '{title[:70]}' in category '{self.category}'"
        )

        def _create() -> dict:
            category_id = self.client.get_or_create_category(self.category)
            tag_ids = [self.client.get_or_create_tag(t) for t in self.tags]
            return self.client.create_post(
                title=title,
                content=html,
                status=self.delivery,  # "draft" unless NEWSLETTER_DELIVERY=publish
                categories=[category_id],
                tags=tag_ids or None,
                excerpt=issue.intro or None,
            )

        try:
            post = await asyncio.to_thread(_create)  # the client is synchronous
        except (
            Exception
        ) as e:  # noqa: BLE001 - every client failure is a publish failure
            raise PublishError(f"WordPress draft failed: {e}") from e

        post_id = str((post or {}).get("id") or "").strip()
        if not post_id:
            raise PublishError(f"WordPress returned no post id: {str(post)[:200]}")
        url = (post or {}).get("link") or None
        status = str((post or {}).get("status") or "draft")
        logger.info(
            f"WordPress: draft {post_id} ({status})" + (f" at {url}" if url else "")
        )
        return PublishResult(
            publisher=self.name,
            external_id=post_id,
            url=url,
            status=status,
            slug=(post or {}).get("slug") or None,
        )

    async def test_connection(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self.client.test_connection))
        except Exception as e:  # noqa: BLE001
            logger.error(f"WordPress connection failed: {e}")
            return False
