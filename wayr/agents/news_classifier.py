"""News Classifier Agent.

Decides whether an article is NEWS or a NON-NEWS subcategory (Deal/Opinion/Report/Review/Other).
"""

import re

from wayr.agents.base import BaseAgent
from wayr.models import RSSArticle, NewsTypeResult
from wayr.models.articles import ContentType
from wayr.utils import get_logger

logger = get_logger(__name__)


_SUBCATEGORY_MAP = {
    "deal": ContentType.DEAL,
    "opinion": ContentType.OPINION,
    "report": ContentType.REPORT,
    "review": ContentType.REVIEW,
    "other": ContentType.OTHER,
}


_BRAND_CONTEXT = """

<source_context>
This item is a first-party announcement published by the company itself ({source}); it is not press coverage. Its title states what was announced; its body is often marketing copy or a customer quote rather than reporting.

Title: "{title}"

Judge the announcement, not the prose around it:
- Something new, concrete and confirmed — a product, model, feature, agreement, partnership, pricing or policy change, hire, or result — is NEWS. The company's own statement is the official source, so the sourcing criterion is met by the announcement itself.
- It is not Review: nobody is evaluating a product here; the company is announcing one.
- It is not Deal: Deal means promotions and discounts, not commercial agreements or partnerships.
- The title is the announcement. If it names something new — a model, product, version, feature or agreement — that is the news, even when the body only shows it in use or quotes the people using it. Judge "new" from the title, not from how the body reads.
- If the title announces nothing new — a feature tour, a customer story, a "how we think about X" post about an existing product — it is NON-NEWS, Other.
</source_context>"""


def _source_context(article: RSSArticle) -> str:
    """Extra context for brand sources only. Press items get an empty string,
    so their prompt is byte-identical to the one the evals were built on."""
    if article.source_origin != "brand":
        return ""
    return _BRAND_CONTEXT.format(
        source=article.source or "the company", title=article.title.strip()
    )


def _format_article(article: RSSArticle) -> str:
    """Matches the workbench input: raw scraped content only. No title fallback —
    discovery drops articles with insufficient content before this point."""
    return article.content or ""


class NewsClassifierAgent(BaseAgent):
    """Agent B — classifies NEWS vs NON-NEWS and picks a non-news subcategory."""

    def __init__(
        self,
        api_key: str,
        model: str,
        provider: str = "openai",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ):
        super().__init__(
            name="NewsClassifierAgent",
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def classify(self, article: RSSArticle) -> NewsTypeResult:
        """Classify the article as NEWS or a non-news subcategory."""
        prompt = self.load_prompt(
            "news_classifier",
            ARTICLE_CONTENT=_format_article(article),
            SOURCE_CONTEXT=_source_context(article),
        )

        response = await self._call_llm(user_prompt=prompt)
        block = self._extract_tag(response, "classification") or ""

        classification_match = re.search(
            r"Classification:\s*(NEWS|NON-NEWS)", block, re.IGNORECASE
        )
        category_match = re.search(r"Category:\s*([^\n]+)", block, re.IGNORECASE)

        if not classification_match:
            logger.warning(
                f"NewsClassifierAgent: could not parse classification for "
                f"'{article.title[:60]}...'. Defaulting to OTHER."
            )
            return NewsTypeResult(
                content_type=ContentType.OTHER,
                reasoning="Unparseable response from model",
            )

        is_news = classification_match.group(1).strip().upper() == "NEWS"

        if is_news:
            return NewsTypeResult(
                content_type=ContentType.NEWS,
                reasoning="NEWS",
            )

        subcategory = (
            category_match.group(1).strip().lower() if category_match else "other"
        )
        subcategory = subcategory.replace('"', "").replace("'", "").strip()
        content_type = _SUBCATEGORY_MAP.get(subcategory, ContentType.OTHER)

        if subcategory not in _SUBCATEGORY_MAP:
            logger.warning(
                f"NewsClassifierAgent: unknown subcategory '{subcategory}' for "
                f"'{article.title[:60]}...'. Falling back to OTHER."
            )

        return NewsTypeResult(
            content_type=content_type,
            reasoning=f"Non-news: {subcategory}",
        )
