"""Industry Classifier Agent.

Decides whether an article's primary domain belongs to the configured industry.
"""

from wayr.agents.base import BaseAgent
from wayr.models import RSSArticle, IndustryFilterResult
from wayr.utils import get_logger

logger = get_logger(__name__)


def _format_article(article: RSSArticle) -> str:
    """Render an RSSArticle as text for the prompt.

    Matches the workbench input the rewritten prompt was validated against:
    raw scraped content only. Title is NOT used as fallback — discovery drops
    articles with insufficient content before they reach classification.
    """
    return article.content or ""


class IndustryClassifierAgent(BaseAgent):
    """Determines if an article belongs to the target industry."""

    def __init__(
        self,
        api_key: str,
        model: str,
        industry: str,
        provider: str = "openai",
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ):
        super().__init__(
            name="IndustryClassifierAgent",
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.industry = industry

    async def check(self, article: RSSArticle) -> IndustryFilterResult:
        """Classify whether the article belongs to the configured industry."""
        prompt = self.load_prompt(
            "industry_classifier",
            INDUSTRY=self.industry,
            ARTICLE=_format_article(article),
        )

        response = await self._call_llm(user_prompt=prompt)

        classification = self._extract_tag(response, "classification") or ""

        belongs = classification.strip().lower().startswith("yes")

        if not classification:
            logger.warning(
                f"IndustryClassifierAgent: no <classification> tag in response for "
                f"'{article.title[:60]}...'. Defaulting to belongs=False."
            )

        reasoning = (
            f"Belongs to {self.industry}"
            if belongs
            else f"Rejected — not {self.industry}"
        )

        return IndustryFilterResult(
            belongs_to_industry=belongs,
            reasoning=reasoning,
        )
