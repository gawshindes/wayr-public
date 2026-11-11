"""Tests for the NewsletterAgent compile call and the source-blurb summary method."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from wayr.agents.newsletter import NewsletterAgent, _clean_emoji
from wayr.agents.summary import SummaryAgent
from wayr.models.newsletter import (
    NewsletterIssue,
    NewsletterSection,
    NewsletterStory,
    SectionSpec,
    Tier,
)


def agent() -> NewsletterAgent:
    return NewsletterAgent(api_key="g", model="gemini-flash-latest", provider="google")


def story(h, tier=Tier.FEATURE, blurb="Facts.") -> NewsletterStory:
    return NewsletterStory(
        url_hash=h,
        url=f"https://ex.com/{h}",
        source="Src",
        source_title=f"{h} title",
        tier=tier,
        blurb=blurb,
    )


def sec(name, style, *stories, tier="feature", type="stories"):
    kw = dict(name=name, type=type)
    if type == "stories":
        kw.update(style=style, tier=tier)
    return NewsletterSection(spec=SectionSpec(**kw), stories=list(stories))


def issue(*sections) -> NewsletterIssue:
    return NewsletterIssue(
        title="Det",
        subject_line="Det",
        intro="Det intro",
        date_label="September 13, 2026",
        sections=list(sections),
        generated_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        platform_name="WAYR",
        ai_disclosure_notice="",
    )


def reply(stories, subject="Big day", title="Big day", intro="Lead is X."):
    return json.dumps(
        {"subject_line": subject, "title": title, "intro": intro, "stories": stories}
    )


class TestPayload:
    def test_excludes_placeholders_and_link_only(self):
        i = issue(
            sec("F", "feature", story("a")),
            sec("P", None, type="placeholder"),
            sec("L", "link_only", story("l", tier=Tier.ROUNDUP), tier="roundup"),
            sec("B", "brief", story("b")),
        )
        rows = NewsletterAgent.payload(i)
        assert [r["id"] for r in rows] == ["a", "b"]
        assert rows[0] == {
            "id": "a",
            "section": "F",
            "style": "feature",
            "source_title": "a title",
            "source": "Src",
            "category": None,
            "blurb": "Facts.",
            "commentary": True,
        }


class TestCompile:
    @pytest.mark.asyncio
    async def test_writes_issue_and_per_style_fields(self):
        i = issue(
            sec("F", "feature", story("a")),
            sec("B", "brief", story("b")),
            sec("Q", "quick_hit", story("q")),
        )
        a = agent()
        a._call_llm = AsyncMock(
            return_value=reply(
                {
                    "a": {
                        "emoji": "🚀",
                        "headline": "Thing ships",
                        "commentary": "It matters.",
                        "anchor_text": "IGNORED",
                    },
                    "b": {
                        "emoji": "📰",
                        "headline": "Other thing",
                        "anchor_text": "Acme",
                        "commentary": "IGNORED",
                    },
                    "q": {"emoji": "⚡", "headline": "Quick one"},
                }
            )
        )
        out = await a.compile(i, readers="CTOs.")
        assert (
            out.llm_generated
            and out.subject_line == "Big day"
            and out.intro == "Lead is X."
        )
        fa, fb, fq = (
            out.sections[0].stories[0],
            out.sections[1].stories[0],
            out.sections[2].stories[0],
        )
        assert (fa.emoji, fa.headline, fa.commentary, fa.anchor_text) == (
            "🚀",
            "Thing ships",
            "It matters.",
            None,
        )
        assert (fb.emoji, fb.headline, fb.anchor_text, fb.commentary) == (
            "📰",
            "Other thing",
            "Acme",
            None,
        )
        assert (fq.emoji, fq.headline) == ("⚡", "Quick one")
        assert fa.blurb == "Facts."  # never rewritten
        prompt = a._call_llm.await_args.kwargs["user_prompt"]
        assert "CTOs." in prompt and '"id": "a"' in prompt
        assert a._call_llm.await_args.kwargs["json_output"] is True

    @pytest.mark.asyncio
    async def test_missing_story_keeps_fallbacks_and_still_succeeds(self):
        i = issue(sec("F", "feature", story("a"), story("z")))
        a = agent()
        a._call_llm = AsyncMock(return_value=reply({"a": {"headline": "Written"}}))
        out = await a.compile(i)
        assert out.llm_generated
        assert out.sections[0].stories[1].headline is None
        assert out.sections[0].stories[1].display_headline == "z title"

    @pytest.mark.asyncio
    async def test_extra_ids_ignored(self):
        i = issue(sec("F", "feature", story("a")))
        a = agent()
        a._call_llm = AsyncMock(
            return_value=reply({"a": {"headline": "H"}, "ghost": {"headline": "G"}})
        )
        out = await a.compile(i)
        assert out.sections[0].stories[0].headline == "H"

    @pytest.mark.asyncio
    async def test_no_stories_written_raises(self):
        i = issue(sec("F", "feature", story("a")))
        a = agent()
        a._call_llm = AsyncMock(return_value=reply({"other": {"headline": "x"}}))
        with pytest.raises(ValueError, match="wrote none"):
            await a.compile(i)

    @pytest.mark.asyncio
    async def test_malformed_reply_raises(self):
        i = issue(sec("F", "feature", story("a")))
        a = agent()
        a._call_llm = AsyncMock(return_value="not json at all")
        with pytest.raises(Exception):
            await a.compile(i)

    @pytest.mark.asyncio
    async def test_stories_not_an_object_raises(self):
        i = issue(sec("F", "feature", story("a")))
        a = agent()
        a._call_llm = AsyncMock(
            return_value=json.dumps({"subject_line": "s", "stories": []})
        )
        with pytest.raises(ValueError, match="must be an object"):
            await a.compile(i)

    @pytest.mark.asyncio
    async def test_nothing_to_write_returns_unchanged_without_calling_model(self):
        i = issue(sec("L", "link_only", story("l", tier=Tier.ROUNDUP), tier="roundup"))
        a = agent()
        a._call_llm = AsyncMock()
        out = await a.compile(i)
        assert not out.llm_generated and out.title == "Det"
        a._call_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_issue_fields_keep_deterministic_values(self):
        i = issue(sec("F", "feature", story("a")))
        a = agent()
        a._call_llm = AsyncMock(
            return_value=reply({"a": {"headline": "H"}}, subject="  ", intro="")
        )
        out = await a.compile(i)
        assert out.subject_line == "Det" and out.intro == "Det intro"


class TestEmojiCleaning:
    def test_accepts_emoji_rejects_words(self):
        assert _clean_emoji("🚀") == "🚀"
        assert _clean_emoji(" 🧠 brain") == "🧠"
        assert _clean_emoji("rocket") is None
        assert _clean_emoji("") is None and _clean_emoji(None) is None


class TestSummaryFromSource:
    def sa(self) -> SummaryAgent:
        return SummaryAgent(api_key="g", model="gemini-flash-latest", provider="google")

    @pytest.mark.asyncio
    async def test_writes_blurb_from_content(self):
        s = self.sa()
        s._call_llm = AsyncMock(return_value="  A tight blurb.  ")
        assert (
            await s.generate_summary_from_source("T", "<p>Body text here.</p>")
            == "A tight blurb."
        )
        prompt = s._call_llm.await_args.kwargs["user_prompt"]
        assert (
            "Body text here." in prompt
            and "<p>" not in prompt
            and "do not repeat" in prompt
        )

    @pytest.mark.asyncio
    async def test_empty_content_no_call(self):
        s = self.sa()
        s._call_llm = AsyncMock()
        assert await s.generate_summary_from_source("T", "   ") == ""
        s._call_llm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_model_failure_falls_back_to_first_paragraph(self):
        s = self.sa()
        s._call_llm = AsyncMock(side_effect=RuntimeError("down"))
        out = await s.generate_summary_from_source("T", "First para.\n\nSecond para.")
        assert out == "First para."


class TestVoice:
    def test_default_voice_is_the_plain_house_voice(self):
        from wayr.agents.newsletter import resolve_voice

        voice, source = resolve_voice(None, None)
        assert source == "default" and "No hype" in voice and "Opening" in voice

    def test_inline_beats_file_beats_default(self, tmp_path):
        from wayr.agents.newsletter import resolve_voice

        f = tmp_path / "v.md"
        f.write_text("# Ours\nBe funny. <!-- note -->\n")
        assert resolve_voice("Be terse.", str(f)) == ("Be terse.", "inline")
        assert resolve_voice(None, str(f)) == ("# Ours\nBe funny.", "file")

    def test_missing_or_empty_file_raises(self, tmp_path):
        from wayr.agents.newsletter import resolve_voice

        with pytest.raises(FileNotFoundError):
            resolve_voice(None, str(tmp_path / "nope.md"))
        empty = tmp_path / "e.md"
        empty.write_text("<!-- nothing -->\n")
        with pytest.raises(ValueError):
            resolve_voice(None, str(empty))

    def test_voice_reaches_the_prompt(self, tmp_path):
        from wayr.agents.newsletter import NewsletterAgent

        f = tmp_path / "v.md"
        f.write_text("Welcome, humans.")
        a = NewsletterAgent(
            api_key="k", model="m", provider="openai", voice_file=str(f)
        )
        assert a.voice_source == "file"
        prompt = a.load_prompt(
            "newsletter",
            PLATFORM_NAME="P",
            DATE="D",
            READERS="R",
            VOICE=a.voice,
            STORIES_JSON="[]",
        )
        assert "<voice>\nWelcome, humans.\n</voice>" in prompt


class TestStructuralFlags:
    def _issue(self, commentary=True):
        from datetime import datetime, timezone
        from wayr.models.newsletter import (
            NewsletterIssue,
            NewsletterSection,
            NewsletterStory,
            SectionSpec,
            Tier,
        )

        spec = SectionSpec(
            name="Top", style="feature", tier="feature", commentary=commentary
        )
        st = NewsletterStory(
            url_hash="h1",
            url="https://x/1",
            source="S",
            source_title="T",
            tier=Tier.FEATURE,
            blurb="b",
        )
        return NewsletterIssue(
            title="t",
            subject_line="s",
            intro="fallback",
            date_label="d",
            sections=[NewsletterSection(spec=spec, stories=[st])],
            generated_at=datetime.now(timezone.utc),
            platform_name="p",
        )

    def test_payload_carries_the_commentary_flag(self):
        assert NewsletterAgent.payload(self._issue(True))[0]["commentary"] is True
        assert NewsletterAgent.payload(self._issue(False))[0]["commentary"] is False

    @pytest.mark.asyncio
    async def test_flags_win_over_what_the_model_returns(self):
        a = NewsletterAgent(api_key="k", model="m", provider="openai")
        reply = json.dumps(
            {
                "subject_line": "s",
                "title": "t",
                "intro": "an opening anyway",
                "stories": {
                    "h1": {
                        "emoji": "x",
                        "headline": "H",
                        "commentary": "written anyway",
                    }
                },
            }
        )
        a._call_llm = AsyncMock(return_value=reply)
        issue = await a.compile(self._issue(commentary=False), intro=False)
        assert issue.intro == ""
        assert issue.sections[0].stories[0].commentary is None
        assert issue.sections[0].stories[0].headline == "H"

    def test_schema_flags_load_from_yaml(self, tmp_path):
        from wayr.models.newsletter import SectionSchema

        f = tmp_path / "s.yaml"
        f.write_text(
            "intro: false\nsections:\n  - {name: A, type: stories, style: feature, tier: feature, commentary: false}\n"
        )
        sch = SectionSchema.load(str(f))
        assert sch.intro is False and sch.sections[0].commentary is False
        assert SectionSchema.load("newsletters/example/sections.yaml").intro is False
