"""Content extraction service for full article content."""

from typing import Optional
from newspaper import Article
from wayr.utils import get_logger

logger = get_logger(__name__)


class ContentExtractor:
    """Extracts full article content from URLs."""

    def __init__(self, timeout: int = 30):
        """
        Initialize content extractor.

        Args:
            timeout: Request timeout in seconds
        """
        self.timeout = timeout

    async def extract(self, url: str) -> Optional[str]:
        """
        Extract full article content from URL.

        Args:
            url: Article URL

        Returns:
            Extracted article text or None if extraction fails
        """
        try:
            logger.debug(f"Extracting content from: {url}")

            # Use newspaper3k for extraction
            article = Article(url)
            article.download()
            article.parse()

            if article.text:
                logger.debug(f"Successfully extracted {len(article.text)} characters")
                return article.text
            else:
                logger.warning(f"No content extracted from {url}")
                return None

        except Exception as e:
            logger.warning(f"Error extracting content from {url}: {e}")
            return None

    async def extract_with_metadata(self, url: str) -> dict:
        """
        Extract article content along with metadata.

        Args:
            url: Article URL

        Returns:
            Dictionary with content, title, authors, etc.
        """
        try:
            article = Article(url)
            article.download()
            article.parse()

            return {
                "content": article.text,
                "title": article.title,
                "authors": article.authors,
                "publish_date": article.publish_date,
                "top_image": article.top_image,
                "meta_description": article.meta_description,
            }

        except Exception as e:
            logger.error(f"Error extracting metadata from {url}: {e}")
            return {}
