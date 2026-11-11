"""Publishing Agent - Publishes articles to WordPress."""

from typing import List
from datetime import datetime, timezone
from wayr.models import EditedArticle, PublishedArticle
from wayr.services import WordPressClient
from wayr.utils import get_logger

logger = get_logger(__name__)


class PublishingAgent:
    """Agent responsible for publishing articles to WordPress."""

    def __init__(
        self,
        wordpress_url: str,
        wordpress_username: str,
        wordpress_app_password: str,
        taxonomy_cache_file: str = "data/wp_taxonomy.json",
    ):
        """
        Initialize Publishing Agent.

        Args:
            wordpress_url: WordPress site URL
            wordpress_username: WordPress username
            wordpress_app_password: WordPress application password
            taxonomy_cache_file: Path to persistent name→ID cache for WP categories/tags
        """
        self.name = "PublishingAgent"
        self.wp_client = WordPressClient(
            wordpress_url,
            wordpress_username,
            wordpress_app_password,
            taxonomy_cache_file=taxonomy_cache_file,
        )

    async def publish_articles(
        self, edited_articles: List[EditedArticle]
    ) -> List[PublishedArticle]:
        """
        Publish edited articles to WordPress.

        Args:
            edited_articles: List of EditedArticle objects

        Returns:
            List of PublishedArticle objects
        """
        logger.info(f"Publishing {len(edited_articles)} articles to WordPress...")

        published = []
        for edited in edited_articles:
            try:
                result = await self._publish_single(edited)
                if result:
                    published.append(result)
            except Exception as e:
                logger.error(f"Error publishing article '{edited.edited_title}': {e}")
                continue

        logger.info(f"Successfully published {len(published)} articles")
        return published

    async def _publish_single(self, edited: EditedArticle) -> PublishedArticle:
        """
        Publish a single article to WordPress.

        Args:
            edited: EditedArticle to publish

        Returns:
            PublishedArticle object
        """
        try:
            # Get or create categories for the article
            # Use corrected categories if proofreader changed them, otherwise use original
            if edited.corrected_categories:
                effective_categories = edited.corrected_categories
                logger.info(
                    f"Using corrected categories: {effective_categories} "
                    f"(was: {edited.generated.classified.categories})"
                )
            else:
                effective_categories = edited.generated.classified.categories

            category_ids = []
            for cat in effective_categories:
                cat_id = self.wp_client.get_or_create_category(cat)
                category_ids.append(cat_id)

            # Get or create tag for the source (e.g., "TechCrunch", "The Verge")
            source_name = edited.generated.classified.original.source
            tag_id = self.wp_client.get_or_create_tag(source_name)
            logger.info(f"Using source tag: {source_name}")

            # Use LLM-generated summary as excerpt, fallback to HTML extraction
            excerpt = (
                edited.summary
                if edited.summary
                else self._extract_excerpt(edited.edited_content)
            )

            # Create WordPress post (use edited title and content)
            logger.info(f"Publishing to WordPress: {edited.edited_title}")

            post_data = self.wp_client.create_post(
                title=edited.edited_title,
                content=edited.edited_content,
                status="publish",
                categories=category_ids,
                tags=[tag_id],
                excerpt=excerpt,
            )

            # Extract post ID and URL
            post_id = post_data["id"]
            post_url = post_data["link"]

            logger.info(f"Published successfully - ID: {post_id}, URL: {post_url}")

            return PublishedArticle(
                edited=edited,
                wordpress_post_id=post_id,
                wordpress_url=post_url,
                published_at=datetime.now(timezone.utc),
                status="publish",
            )

        except Exception as e:
            logger.error(f"Publishing failed for '{edited.edited_title}': {e}")
            raise

    def _extract_excerpt(self, html_content: str, max_length: int = 200) -> str:
        """
        Extract excerpt from HTML content.

        Args:
            html_content: HTML content
            max_length: Maximum excerpt length

        Returns:
            Excerpt text
        """
        # Simple excerpt extraction - remove HTML tags and truncate
        import re

        text = re.sub(r"<[^>]+>", "", html_content)
        text = text.strip()

        if len(text) > max_length:
            text = text[:max_length].rsplit(" ", 1)[0] + "..."

        return text

    def test_connection(self) -> bool:
        """
        Test WordPress connection.

        Returns:
            True if connection successful
        """
        return self.wp_client.test_connection()
