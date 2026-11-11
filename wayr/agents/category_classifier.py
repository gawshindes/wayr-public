"""Category Classifier Agent.

Assigns primary + secondary categories to news articles from a configured,
industry-specific list.
"""

import re
from typing import List

from wayr.agents.base import BaseAgent
from wayr.models import RSSArticle, CategoryResult
from wayr.utils import get_logger

logger = get_logger(__name__)


def _format_article(article: RSSArticle) -> str:
    """Raw scraped content only, matching the workbench input the prompt was
    validated against. No title/summary/tags — titles can be deceptive and
    discovery guarantees content is present by this point."""
    return article.content or ""


def _render_allowed_list(categories: List[str]) -> str:
    return "\n".join(f"- {c}" for c in categories)


def _fuzzy_match(value: str, allowed: List[str]) -> str:
    """Return canonical category name from allowed list, else 'General' fallback."""
    if not value:
        return "General" if "General" in allowed else allowed[-1]
    normalized = value.strip().strip('"').strip("'")
    for cat in allowed:
        if cat.lower() == normalized.lower():
            return cat
    # Try contains match (e.g. "Mobile Apps" vs "Mobile & Apps")
    for cat in allowed:
        if normalized.lower() in cat.lower() or cat.lower() in normalized.lower():
            return cat
    return "General" if "General" in allowed else allowed[-1]


class CategoryClassifierAgent(BaseAgent):
    """Assigns primary + secondary news categories."""

    def __init__(
        self,
        api_key: str,
        model: str,
        industry: str,
        allowed_categories: List[str],
        provider: str = "openai",
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ):
        super().__init__(
            name="CategoryClassifierAgent",
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.industry = industry
        self.allowed_categories = allowed_categories

    async def categorize(self, article: RSSArticle) -> CategoryResult:
        """Return primary + secondary category picks from the allowed list."""
        prompt = self.load_prompt(
            "category_classifier",
            ARTICLE_CONTENT=_format_article(article),
            INDUSTRY_CONTEXT=self.industry,
            INDUSTRY_CATEGORIES=_render_allowed_list(self.allowed_categories),
        )

        response = await self._call_llm(user_prompt=prompt)
        block = self._extract_tag(response, "categories") or ""

        primary_match = re.search(r"Primary:\s*([^\n]+)", block, re.IGNORECASE)
        secondary_match = re.search(r"Secondary:\s*([^\n]+)", block, re.IGNORECASE)

        primary_raw = primary_match.group(1).strip() if primary_match else ""
        secondary_raw = secondary_match.group(1).strip() if secondary_match else ""

        primary = _fuzzy_match(primary_raw, self.allowed_categories)
        secondary = _fuzzy_match(secondary_raw, self.allowed_categories)

        if primary_raw and primary != primary_raw:
            logger.warning(
                f"CategoryClassifierAgent: primary '{primary_raw}' not in allowed list; "
                f"coerced to '{primary}' for '{article.title[:60]}...'"
            )
        if secondary_raw and secondary != secondary_raw:
            logger.warning(
                f"CategoryClassifierAgent: secondary '{secondary_raw}' not in allowed list; "
                f"coerced to '{secondary}' for '{article.title[:60]}...'"
            )

        return CategoryResult(
            primary=primary,
            secondary=secondary,
            reasoning=f"Primary: {primary}; Secondary: {secondary}",
        )
