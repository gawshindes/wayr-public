"""Summary Agent - Generates concise summaries for SEO and social media."""

import html
import re
from typing import List

from wayr.agents.base import BaseAgent
from wayr.models import EditedArticle
from wayr.utils import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = "You are a tech-savvy social media writer. Write concise, factually accurate summaries. Respond with ONLY the summary — no preamble."

_RULES = """Rules:
- Write a flowing paragraph, NOT bullet points
- Include specific facts, numbers, names, or dates from the article
- First sentence should capture the key news angle NOT obvious from the title
- Aim for 200-280 characters. If a third sentence adds value, it's okay to slightly exceed 280
- Professional, informative tone for a tech-savvy audience
- All facts must come directly from the article — do not infer or hallucinate"""


class SummaryAgent(BaseAgent):
    """Agent that generates summaries used for WordPress excerpts and social media posts.

    Runs after proofreading, before publishing. Sets `summary` on each EditedArticle.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        provider: str = "openai",
        temperature: float = 0.7,
        max_tokens: int = 800,
    ):
        super().__init__(
            name="SummaryAgent",
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def generate_summaries(
        self, articles: List[EditedArticle]
    ) -> List[EditedArticle]:
        """Generate summaries for a list of edited articles.

        Mutates each article's `summary` field in place and returns the same list.
        """
        logger.info(f"Generating summaries for {len(articles)} articles...")

        for article in articles:
            try:
                summary = await self._generate_summary(article)
                article.summary = summary
                logger.info(f"  ✓ Summary for: {article.edited_title[:50]}...")
            except Exception as e:
                logger.error(f"  ✗ Summary failed for '{article.edited_title}': {e}")
                # Fallback: first paragraph extraction
                article.summary = self._extract_fallback_summary(article.edited_content)

        logger.info(f"Generated {len(articles)} summaries")
        return articles

    async def _generate_summary(self, article: EditedArticle) -> str:
        """Generate a summary for a single article using LLM."""
        article_text = self._clean_article_text(article.edited_content)

        prompt = f"""Write a 2-3 sentence social media summary of this tech article. The article title will be shown separately in the link card, so do NOT repeat the title.

Rules:
- Write a flowing paragraph, NOT bullet points
- Include specific facts, numbers, names, or dates from the article
- First sentence should capture the key news angle NOT obvious from the title
- Aim for 200-280 characters. If a third sentence adds value, it's okay to slightly exceed 280
- Professional, informative tone for a tech-savvy audience
- All facts must come directly from the article — do not infer or hallucinate

Example of a GOOD first sentence (title: "OpenAI Launches GPT-5"):
✅ "The new model scores 92% on MMLU-Pro, a 15-point jump over GPT-4o, and is available to all API tiers starting today."
(Adds specific facts not in the title)

❌ "OpenAI has just launched their latest AI model, GPT-5."
(Just restates the title)

Article Title (for context, do not repeat): {article.edited_title}

{article_text[:1500]}

Respond with ONLY the summary paragraph."""

        response = await self._call_llm(user_prompt=prompt, system_prompt=SYSTEM_PROMPT)
        return response.strip()

    async def generate_summary_from_source(self, title: str, content: str) -> str:
        """Newsletter blurb written straight from extracted source content.

        Used by newsletter mode, where no article is ever authored. Returns ""
        when there is no usable content, and falls back to the first paragraph
        if the model call fails — a blurb beats an empty item.
        """
        text = self._clean_article_text(content or "")
        if not text.strip():
            return ""

        prompt = f"""Write a 2-3 sentence newsletter blurb for this story. The headline will be shown separately, so do NOT repeat the title.

{_RULES}

Story title (for context, do not repeat): {title}

{text[:1500]}

Respond with ONLY the blurb paragraph."""
        try:
            response = await self._call_llm(
                user_prompt=prompt, system_prompt=SYSTEM_PROMPT
            )
            return response.strip()
        except Exception as e:
            logger.error(f"Blurb failed for '{title[:60]}': {e}; using first paragraph")
            return self._extract_fallback_summary(text)

    def _clean_article_text(self, html_content: str) -> str:
        """Strip HTML tags and AI disclosure from article content."""
        clean_text = re.sub(r"<[^>]+>", "", html_content)
        clean_text = html.unescape(clean_text)

        paragraphs = clean_text.split("\n\n")
        article_text = "\n\n".join(
            p.strip()
            for p in paragraphs
            if p.strip()
            and not p.strip().startswith("This article was generated by AI")
        )
        return article_text

    def _extract_fallback_summary(
        self, html_content: str, max_length: int = 280
    ) -> str:
        """Fallback: extract first paragraph as summary when LLM fails."""
        clean_text = re.sub(r"<[^>]+>", "", html_content)
        clean_text = html.unescape(clean_text)

        paragraphs = [
            p.strip()
            for p in clean_text.split("\n\n")
            if p.strip()
            and not p.strip().startswith("This article was generated by AI")
        ]

        if not paragraphs:
            return ""

        text = paragraphs[0]
        if len(text) > max_length:
            text = text[:max_length].rsplit(" ", 1)[0] + "..."
        return text
