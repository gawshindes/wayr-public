"""RSS feed fetching service."""

import time
import feedparser
import requests
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Union
from dateutil import parser as date_parser
from wayr.models import RSSArticle, Source
from wayr.utils.text import html_to_text
from wayr.utils import get_logger

if TYPE_CHECKING:
    from wayr.services.feed_ledger import FeedLedger

logger = get_logger(__name__)


def _json_safe(value: Any) -> Any:
    """Coerce feedparser entry values into JSON-serializable structures.

    feedparser returns `time.struct_time` for parsed dates and FeedParserDict
    wrappers for nested structures — neither is directly JSON-safe.
    """
    if isinstance(value, time.struct_time):
        try:
            return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# Every feed fetch is bounded: (connect, read) seconds. A feed that cannot answer
# in that time is recorded as an error and the run moves on.
FETCH_TIMEOUT = (10, 30)
USER_AGENT = "Mozilla/5.0 (compatible; WAYR/1.0; +https://wayr.today)"

# A feed body at least this long is the article, not a teaser: use it as the
# content and skip the page fetch. Medium's feeds carry the whole post; most
# news sites carry ~20 words.
FEED_FULL_TEXT_MIN_WORDS = 200


class RSSFetcher:
    """Fetches and parses RSS feeds."""

    def __init__(
        self,
        feed_urls: Sequence[Union[str, Source]],
        ledger: Optional["FeedLedger"] = None,
    ):
        """
        Initialize RSS fetcher.

        Args:
            feed_urls: The configured sources — `Source` objects, or bare URLs
                which mean rss/press. `type=page` sources are accepted and
                skipped (the listing-page adapter does not exist yet); they are
                warned about once, here, not on every fetch.
            ledger: Optional FeedLedger. When present, each fetch is a conditional
                GET using the feed's stored ETag / Last-Modified (a 304 skips the
                feed entirely), and every entry the feed shows is recorded.
        """
        self.sources: List[Source] = [
            s if isinstance(s, Source) else Source(url=s) for s in feed_urls
        ]
        self._source_by_url = {s.url: s for s in self.sources}
        self.skipped_sources = [s for s in self.sources if s.type != "rss"]
        # The plain URL list every fetch path iterates; page sources are not in it.
        self.feed_urls = [s.url for s in self.sources if s.type == "rss"]
        self.ledger = ledger
        if self.skipped_sources:
            logger.warning(
                f"{len(self.skipped_sources)} page source(s) not supported yet, "
                f"skipped: {', '.join(s.url for s in self.skipped_sources)}"
            )

    async def fetch_all(self, max_articles: Optional[int] = None) -> List[RSSArticle]:
        """
        Fetch articles from all configured RSS feeds.

        Args:
            max_articles: Maximum number of articles to return (None for all)

        Returns:
            List of RSSArticle objects
        """
        all_articles = []

        for feed_url in self.feed_urls:
            try:
                logger.info(f"Fetching RSS feed: {feed_url}")
                articles = await self.fetch_feed(feed_url)
                all_articles.extend(articles)
                logger.info(f"Fetched {len(articles)} articles from {feed_url}")
            except Exception as e:
                logger.error(f"Error fetching feed {feed_url}: {e}")
                continue

        # Sort by published date (most recent first)
        all_articles.sort(key=lambda x: x.published_date, reverse=True)

        # Limit if requested
        if max_articles:
            all_articles = all_articles[:max_articles]

        logger.info(f"Total articles fetched: {len(all_articles)}")
        return all_articles

    async def fetch_feed(self, feed_url: str) -> List[RSSArticle]:
        """
        Fetch articles from a single RSS feed.

        Args:
            feed_url: RSS feed URL

        Returns:
            List of RSSArticle objects
        """
        # Conditional GET: send the validators we stored last time, if any.
        # The HTTP is ours, not feedparser's: feedparser.parse(url) fetches
        # with no timeout, and one dead connection held a whole prework for
        # 2½ hours (tech-links, 2026-09-24). requests gets a connect/read
        # timeout; feedparser only parses the bytes.
        state = self.ledger.feed_state(feed_url) if self.ledger else {}
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        }
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
        try:
            resp = requests.get(feed_url, headers=headers, timeout=FETCH_TIMEOUT)
        except Exception:
            if self.ledger:
                self.ledger.record_fetch(feed_url, status="error")
            raise

        status = resp.status_code
        if status == 304:
            logger.info(f"Feed unchanged (304), skipping: {feed_url}")
            if self.ledger:
                self.ledger.record_fetch(feed_url, status=304)
            return []
        if status != 200:
            if self.ledger:
                self.ledger.record_fetch(feed_url, status=status)
            raise RuntimeError(f"HTTP {status} fetching {feed_url}")

        feed = feedparser.parse(resp.content)
        feed_etag = resp.headers.get("ETag")
        feed_modified = resp.headers.get("Last-Modified")

        if feed.bozo:
            logger.warning(f"Feed {feed_url} has parsing issues: {feed.bozo_exception}")

        articles = []

        # Extract source name from feed
        source_name = feed.feed.get("title", feed_url)
        source = self._source_by_url.get(feed_url) or Source(url=feed_url)

        for entry in feed.entries:
            try:
                article = self._parse_entry(entry, source_name, source)
                if article:
                    articles.append(article)
            except Exception as e:
                logger.warning(f"Error parsing entry: {e}")
                continue

        if self.ledger:
            # feedparser exposes the response validators as attributes when the
            # server sent them; absent ones simply stay None.
            self.ledger.record_fetch(
                feed_url,
                status=200,
                etag=feed_etag,
                last_modified=feed_modified,
                item_count=len(articles),
            )
            new, known = self.ledger.observe(articles, feed_url)
            logger.info(
                f"Ledger: {feed_url} → {len(new)} never seen, {known} already known"
            )

        return articles

    def _parse_entry(
        self, entry, source_name: str, source: Optional[Source] = None
    ) -> Optional[RSSArticle]:
        """
        Parse a single feed entry into an RSSArticle.

        Args:
            entry: Feed entry from feedparser
            source_name: Name of the source publication

        Returns:
            RSSArticle object or None if parsing fails
        """
        try:
            # Extract title
            title = entry.get("title", "").strip()
            if not title:
                return None

            # Extract URL
            url = entry.get("link", "").strip()
            if not url:
                return None

            # Parse published date
            published_date = self._parse_date(entry)
            if not published_date:
                published_date = datetime.now(timezone.utc)

            # Extract summary/description
            summary = entry.get("summary", "") or entry.get("description", "")

            # Full text the feed carries, if any (content:encoded first).
            body = ""
            if entry.get("content"):
                body = " ".join(c.get("value", "") for c in entry["content"])
            feed_text = html_to_text(body or summary)
            feed_words = len(feed_text.split())
            content = feed_text if feed_words >= FEED_FULL_TEXT_MIN_WORDS else None

            # Extract author
            author = entry.get("author", None)

            # Extract tags
            tags = []
            if "tags" in entry:
                tags = [tag.get("term", "") for tag in entry.tags if tag.get("term")]

            rss_raw = _json_safe(dict(entry))

            return RSSArticle(
                title=title,
                url=url,
                source=source_name,
                published_date=published_date,
                summary=summary,
                content=content,  # feed-carried full text, else extracted later
                feed_content=feed_text or None,
                author=author,
                tags=tags,
                rss_raw=rss_raw,
                feed_url=source.url if source else None,
                source_origin=source.origin if source else None,
            )

        except Exception as e:
            logger.error(f"Error creating RSSArticle: {e}")
            return None

    def _parse_date(self, entry) -> Optional[datetime]:
        """
        Parse date from feed entry.

        Args:
            entry: Feed entry from feedparser

        Returns:
            datetime object or None
        """
        # Try different date fields
        date_fields = ["published_parsed", "updated_parsed", "created_parsed"]

        for field in date_fields:
            if field in entry and entry[field]:
                try:
                    time_struct = entry[field]
                    return datetime(*time_struct[:6])
                except Exception:
                    pass

        # Try parsing date strings
        date_string_fields = ["published", "updated", "created"]
        for field in date_string_fields:
            if field in entry and entry[field]:
                try:
                    return date_parser.parse(entry[field])
                except Exception:
                    pass

        return None
