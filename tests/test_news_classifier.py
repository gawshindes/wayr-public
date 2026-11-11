"""News-type classifier: the brand-origin hint and prompt hygiene."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from wayr.agents.news_classifier import NewsClassifierAgent, _source_context
from wayr.models import RSSArticle
from wayr.models.articles import ContentType


def art(origin=None, title="GPT-6 Astra: The next generation in intelligence for work"):
    return RSSArticle(
        title=title,
        url="https://openai.example/index/astra",
        source="OpenAI News",
        published_date=datetime(2026, 9, 22, tzinfo=timezone.utc),
        content="At OpenAI, Astra was rolled out internally weeks before launch.",
        source_origin=origin,
    )


def agent():
    return NewsClassifierAgent(api_key="k", model="m", provider="openai")


class TestSourceContext:
    def test_press_and_untyped_get_nothing(self):
        assert _source_context(art("press")) == ""
        assert _source_context(art(None)) == ""

    def test_brand_gets_title_and_source(self):
        ctx = _source_context(art("brand"))
        assert ctx.startswith("\n\n<source_context>")
        assert (
            'Title: "GPT-6 Astra: The next generation in intelligence for work"' in ctx
        )
        assert "(OpenAI News)" in ctx and "not Deal" in ctx and "not Review" in ctx


class TestPrompt:
    def test_press_prompt_has_no_context_and_no_leak(self):
        a = agent()
        prompt = a.load_prompt(
            "news_classifier",
            ARTICLE_CONTENT="body",
            SOURCE_CONTEXT=_source_context(art("press")),
        )
        assert "<source_context>" not in prompt
        assert "{{" not in prompt
        assert "Here is the article:\n\n<article>\nbody\n</article>" in prompt

    def test_brand_prompt_carries_the_block_before_the_article(self):
        a = agent()
        prompt = a.load_prompt(
            "news_classifier",
            ARTICLE_CONTENT="body",
            SOURCE_CONTEXT=_source_context(art("brand")),
        )
        assert prompt.index("<source_context>") < prompt.index("<article>")
        assert "{{" not in prompt

    def test_prompt_placeholders_are_exactly_the_two_the_agent_passes(self):
        import re
        from pathlib import Path

        text = (Path("wayr/prompts/news_classifier.md")).read_text()
        assert set(re.findall(r"\{\{(\w+)\}\}", text)) == {
            "ARTICLE_CONTENT",
            "SOURCE_CONTEXT",
        }


class TestClassify:
    @pytest.mark.asyncio
    async def test_brand_article_round_trip(self):
        a = agent()
        seen = {}

        async def fake(user_prompt, **kw):
            seen["prompt"] = user_prompt
            return (
                "<analysis>first-party launch</analysis>\n<classification>\n"
                "Classification: NEWS\nCategory: N/A\nReasoning: NEWS\n</classification>"
            )

        a._call_llm = AsyncMock(side_effect=fake)
        result = await a.classify(art("brand"))
        assert result.content_type == ContentType.NEWS
        assert "<source_context>" in seen["prompt"] and "{{" not in seen["prompt"]
