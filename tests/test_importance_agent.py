"""Tests for ImportanceAgent parsing, normalization and the criteria seam."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from wayr.agents.importance import ImportanceAgent, default_criteria
from wayr.models import ClassifiedArticle, RSSArticle
from wayr.models.articles import ContentType


def agent(**kw) -> ImportanceAgent:
    a = ImportanceAgent(
        api_key="g-fake", model="gemini-flash-latest", provider="google", **kw
    )
    return a


def story(title="OpenAI ships GPT-6", category="AI") -> ClassifiedArticle:
    return ClassifiedArticle(
        original=RSSArticle(
            title=title,
            url="https://example.com/x",
            source="Ex",
            published_date=datetime(2026, 9, 1),
            content="w " * 80,
        ),
        content_type=ContentType.NEWS,
        categories=[category],
    )


def reply(score, reasoning="Affects every reader's stack."):
    return (
        "<analysis>brief</analysis>\n<importance>\n"
        f"Score: {score}\nReasoning: {reasoning}\n</importance>"
    )


class TestParsing:
    @pytest.mark.asyncio
    async def test_integer_score_normalized_to_unit_interval(self):
        a = agent()
        a._call_llm = AsyncMock(return_value=reply(8))
        r = await a.score(story())
        assert r.score == 0.8 and r.parsed is True
        assert r.reasoning == "Affects every reader's stack."

    @pytest.mark.asyncio
    async def test_decimal_score_accepted(self):
        a = agent()
        a._call_llm = AsyncMock(return_value=reply("7.5"))
        assert (await a.score(story())).score == 0.75

    @pytest.mark.asyncio
    async def test_out_of_range_is_clamped(self):
        a = agent()
        a._call_llm = AsyncMock(return_value=reply(14))
        assert (await a.score(story())).score == 1.0

    @pytest.mark.asyncio
    async def test_zero_is_a_valid_score(self):
        a = agent()
        a._call_llm = AsyncMock(return_value=reply(0))
        r = await a.score(story())
        assert r.score == 0.0 and r.parsed is True

    @pytest.mark.asyncio
    async def test_unparseable_reply_gives_neutral_unparsed_score(self):
        a = agent()
        a._call_llm = AsyncMock(return_value="I cannot decide.")
        r = await a.score(story())
        assert r.score == 0.5 and r.parsed is False
        assert "Unparseable" in r.reasoning

    @pytest.mark.asyncio
    async def test_multiline_reasoning_captured(self):
        a = agent()
        a._call_llm = AsyncMock(return_value=reply(6, "Line one.\nLine two."))
        assert (await a.score(story())).reasoning == "Line one.\nLine two."


class TestCriteriaSeam:
    def test_default_criteria_derived_from_industry(self):
        a = agent(industry="Climate")
        assert "Climate" in a.criteria
        assert a.criteria == default_criteria("Climate")

    def test_blank_criteria_falls_back_to_default(self):
        assert agent(industry="Fintech", criteria="   ").criteria == default_criteria(
            "Fintech"
        )

    @pytest.mark.asyncio
    async def test_custom_criteria_reach_the_prompt(self):
        custom = "Readers are hospital CIOs; only clinical-workflow stories matter."
        a = agent(criteria=custom)
        a._call_llm = AsyncMock(return_value=reply(5))
        await a.score(story())
        prompt = a._call_llm.await_args.kwargs["user_prompt"]
        assert custom in prompt
        assert "hospital CIOs" in prompt

    @pytest.mark.asyncio
    async def test_prompt_carries_title_and_category(self):
        a = agent()
        a._call_llm = AsyncMock(return_value=reply(5))
        await a.score(story(title="Nvidia buys Hugging Face", category="Startup"))
        prompt = a._call_llm.await_args.kwargs["user_prompt"]
        assert "Title: Nvidia buys Hugging Face" in prompt
        assert "Category: Startup" in prompt


class TestCriteriaResolution:
    """inline var → file → packaged default, with comments stripped."""

    def test_default_file_loads_and_substitutes_industry(self):
        text = default_criteria("Climate")
        assert "Climate" in text
        assert "{{INDUSTRY}}" not in text
        assert "<!--" not in text  # instructions stripped

    def test_example_file_is_usable_as_is(self):
        from wayr.agents.importance import _TEMPLATES, resolve_criteria

        text, source = resolve_criteria(
            None, str(_TEMPLATES / "importance_criteria.example.md"), "x"
        )
        assert source == "file"
        assert "<!--" not in text
        for heading in (
            "Who reads this",
            "Prioritize",
            "Deprioritize",
            "Calibration examples",
        ):
            assert heading in text, f"example is missing the '{heading}' section"

    def test_inline_beats_file(self, tmp_path):
        f = tmp_path / "c.md"
        f.write_text("FILE CRITERIA")
        a = agent(criteria="INLINE", criteria_file=str(f))
        assert a.criteria == "INLINE" and a.criteria_source == "inline"

    def test_file_beats_default(self, tmp_path):
        f = tmp_path / "c.md"
        f.write_text("<!-- note -->\nReaders are CFOs.\n")
        a = agent(industry="Fintech", criteria_file=str(f))
        assert a.criteria == "Readers are CFOs." and a.criteria_source == "file"
        assert "Fintech" not in a.criteria

    def test_blank_inline_falls_through_to_file(self, tmp_path):
        f = tmp_path / "c.md"
        f.write_text("Readers are CFOs.")
        assert agent(criteria="  ", criteria_file=str(f)).criteria_source == "file"

    def test_default_when_nothing_configured(self):
        a = agent(industry="Fintech")
        assert a.criteria_source == "default" and "Fintech" in a.criteria

    def test_missing_file_is_a_loud_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="does not exist"):
            agent(criteria_file=str(tmp_path / "nope.md"))

    def test_comment_only_file_is_a_loud_error(self, tmp_path):
        f = tmp_path / "c.md"
        f.write_text("<!-- all comment, no content -->")
        with pytest.raises(ValueError, match="empty after stripping"):
            agent(criteria_file=str(f))

    @pytest.mark.asyncio
    async def test_file_criteria_reach_the_prompt(self, tmp_path):
        f = tmp_path / "c.md"
        f.write_text("<!-- x -->\n## Who reads this\nHospital CIOs.")
        a = agent(criteria_file=str(f))
        a._call_llm = AsyncMock(return_value=reply(5))
        await a.score(story())
        prompt = a._call_llm.await_args.kwargs["user_prompt"]
        assert "Hospital CIOs." in prompt and "<!-- x -->" not in prompt
