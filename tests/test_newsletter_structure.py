"""Tests for the section schema, assignment, and renderer."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from wayr.models.newsletter import (
    DEFAULT_SECTIONS_FILE,
    NewsletterIssue,
    NewsletterStory,
    SectionSchema,
    SectionSpec,
    Tier,
)
from wayr.services.newsletter_assign import assign_stories
from wayr.services.newsletter_renderer import NewsletterRenderer


def spec(name, **kw) -> SectionSpec:
    kw.setdefault("type", "stories")
    if kw["type"] == "stories":
        kw.setdefault("style", "brief")
        kw.setdefault("tier", "feature")
    return SectionSpec(name=name, **kw)


def story(
    title, tier=Tier.FEATURE, category="AI", score=0.8, **written
) -> NewsletterStory:
    return NewsletterStory(
        url_hash=title,
        url=f"https://ex.com/{title}",
        source="ExSource",
        source_title=f"{title} (source title)",
        category=category,
        score=score,
        tier=tier,
        **written,
    )


def issue(sections, **kw) -> NewsletterIssue:
    return NewsletterIssue(
        title="T",
        subject_line="S",
        intro="I",
        date_label="September 12, 2026",
        sections=sections,
        generated_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        platform_name="WAYR",
        ai_disclosure_notice="AI-assisted.",
        **kw,
    )


# ────────────────────────────── schema ──────────────────────────────


class TestSectionSpec:
    def test_stories_requires_style_and_tier(self):
        with pytest.raises(ValidationError, match="need a style"):
            SectionSpec(name="x", type="stories", tier="feature")
        with pytest.raises(ValidationError, match="need a tier"):
            SectionSpec(name="x", type="stories", style="brief")

    def test_placeholder_needs_neither(self):
        assert SectionSpec(name="Meme", type="placeholder").is_placeholder

    def test_days_validated_and_normalized(self):
        assert spec("x", days=["Mon", "THU"]).days == ["mon", "thu"]
        with pytest.raises(ValidationError, match="unknown days"):
            spec("x", days=["funday"])

    def test_active_on(self):
        s = spec("x", days=["mon", "thu"])
        assert s.active_on(0) and s.active_on(3) and not s.active_on(1)
        assert spec("every").active_on(6)

    def test_accepts_category(self):
        assert spec("any").accepts_category("AI") and spec("any").accepts_category(None)
        assert spec("star", categories=["*"]).accepts_category("Whatever")
        ai = spec("ai", categories=["AI", "Startup"])
        assert ai.accepts_category("ai") and not ai.accepts_category("Hardware")
        assert not ai.accepts_category(None)

    def test_shows_source_defaults_by_style(self):
        assert spec("f", style="feature").shows_source
        assert not spec("b", style="brief").shows_source
        assert spec("b2", style="brief", show_source=True).shows_source
        assert not spec("f2", style="feature", show_source=False).shows_source


class TestSectionSchema:
    def test_packaged_default_loads(self):
        schema = SectionSchema.load(None)
        assert DEFAULT_SECTIONS_FILE.is_file()
        assert any(s.tier == Tier.ROUNDUP for s in schema.sections)
        assert any(s.tier == Tier.FEATURE for s in schema.sections)

    def test_custom_yaml(self, tmp_path):
        f = tmp_path / "s.yaml"
        f.write_text(
            "sections:\n  - {name: A, type: stories, style: feature, tier: feature, max: 1}\n  - {name: Meme, type: placeholder, days: [mon]}\n"
        )
        schema = SectionSchema.load(str(f))
        assert [s.name for s in schema.sections] == ["A", "Meme"]
        assert [s.name for s in schema.for_day(0)] == ["A", "Meme"]
        assert [s.name for s in schema.for_day(1)] == ["A"]

    def test_rejects_duplicates_and_no_stories(self, tmp_path):
        with pytest.raises(ValidationError, match="duplicate"):
            SectionSchema(sections=[spec("A"), spec("A")])
        with pytest.raises(ValidationError, match="at least one stories"):
            SectionSchema(sections=[SectionSpec(name="P", type="placeholder")])

    def test_missing_or_malformed_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SectionSchema.load(str(tmp_path / "nope.yaml"))
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a list\n")
        with pytest.raises(ValueError, match="top-level 'sections'"):
            SectionSchema.load(str(bad))


# ────────────────────────────── assignment ──────────────────────────────


class TestAssignment:
    def test_tier_routing_and_order(self):
        specs = [
            spec("Top", style="feature", max=1),
            spec("More"),
            spec("Links", style="link_only", tier="roundup"),
        ]
        f = [story("f1", score=0.9), story("f2", score=0.8), story("f3", score=0.7)]
        r = [story("r1", tier=Tier.ROUNDUP, score=0.5)]
        a = assign_stories(specs, f, r)
        by = {s.name: [st.url_hash for st in s.stories] for s in a.sections}
        assert by == {"Top": ["f1"], "More": ["f2", "f3"], "Links": ["r1"]}
        assert a.unplaced == [] and a.placed == 4

    def test_category_routing_with_catch_all_overflow(self):
        specs = [spec("AI", categories=["AI"], max=1), spec("Also", categories=["*"])]
        f = [
            story("a1", category="AI"),
            story("a2", category="AI"),
            story("h1", category="Hardware"),
        ]
        a = assign_stories(specs, f, [])
        by = {s.name: [st.url_hash for st in s.stories] for s in a.sections}
        assert by == {"AI": ["a1"], "Also": ["a2", "h1"]}

    def test_fill_pass_promotes_roundup_into_empty_feature_slots(self):
        # B1's first dump: few featured stories, many roundup, feature slots empty.
        specs = [
            spec("Top", style="feature", max=1),
            spec("Quick hits", style="quick_hit", max=2),
            spec("Links", style="link_only", tier="roundup", max=1),
        ]
        f = [story("f1", score=0.9)]
        r = [story(f"r{i}", tier=Tier.ROUNDUP, score=0.5 - i / 100) for i in range(4)]
        a = assign_stories(specs, f, r)
        by = {s.name: [st.url_hash for st in s.stories] for s in a.sections}
        # strict pass: f1 → Top, r0 → Links; fill pass: r1, r2 → Quick hits
        assert by == {"Top": ["f1"], "Quick hits": ["r1", "r2"], "Links": ["r0"]}
        assert [st.url_hash for st in a.unplaced] == ["r3"]

    def test_fill_pass_demotes_featured_overflow_into_roundup(self):
        specs = [spec("Top", max=1), spec("Links", style="link_only", tier="roundup")]
        a = assign_stories(specs, [story("f1", score=0.9), story("f2", score=0.8)], [])
        by = {s.name: [st.url_hash for st in s.stories] for s in a.sections}
        assert by == {"Top": ["f1"], "Links": ["f2"]}

    def test_fill_pass_still_respects_category_filter(self):
        specs = [spec("AI only", categories=["AI"], tier="feature", max=5)]
        a = assign_stories(
            specs, [], [story("h", tier=Tier.ROUNDUP, category="Hardware")]
        )
        assert [st.url_hash for st in a.unplaced] == ["h"]

    def test_story_appears_once(self):
        specs = [spec("A"), spec("B")]
        a = assign_stories(specs, [story("x")], [])
        assert [s.name for s in a.sections] == ["A"]  # B empty → omitted
        assert a.placed == 1

    def test_unplaced_reported_not_dropped(self):
        specs = [spec("AI only", categories=["AI"], max=1)]
        a = assign_stories(
            specs, [story("a1"), story("a2"), story("h", category="Hardware")], []
        )
        assert [st.url_hash for st in a.unplaced] == ["a2", "h"]

    def test_empty_stories_section_omitted_placeholder_kept(self):
        # "Empty" cannot take the AI story (category filter), so it stays empty and
        # is omitted; the placeholder is kept regardless; "Has" receives the story.
        specs = [
            spec("Empty", categories=["Hardware"]),
            SectionSpec(name="Meme", type="placeholder"),
            spec("Has"),
        ]
        a = assign_stories(specs, [story("x", category="AI")], [])
        assert [s.name for s in a.sections] == ["Meme", "Has"]

    def test_featured_always_wins_the_feature_slot(self):
        # Tier is a preference: the featured story takes the slot even when a
        # higher-scored roundup story is in the list, and only leftover room
        # is filled from the other tier.
        specs = [spec("F", max=1)]
        f = [story("f", score=0.6)]
        r = [story("r", tier=Tier.ROUNDUP, score=0.7)]
        a = assign_stories(specs, f, r)
        assert [st.url_hash for st in a.sections[0].stories] == ["f"]
        assert [st.url_hash for st in a.unplaced] == ["r"]
        # and with no featured story at all, the slot is filled rather than empty
        a = assign_stories(specs, [], r)
        assert [st.url_hash for st in a.sections[0].stories] == ["r"]


# ────────────────────────────── renderer ──────────────────────────────


def render(sections, **kw) -> str:
    return NewsletterRenderer().render(issue(sections, **kw))


def section(sp, *stories):
    from wayr.models.newsletter import NewsletterSection

    return NewsletterSection(spec=sp, stories=list(stories))


class TestRendererStyles:
    def test_feature_headline_unlinked_link_on_source(self):
        html = render(
            [
                section(
                    spec("Top", style="feature"),
                    story(
                        "x",
                        headline="Big news",
                        blurb="Because.",
                        commentary="It matters.",
                        emoji="🚀",
                    ),
                )
            ]
        )
        assert "<h3>🚀 Big news</h3>" in html
        assert '[<a href="https://ex.com/x">ExSource</a>]' in html
        assert "<p>Because.</p>" in html and "<em>It matters.</em>" in html

    def test_feature_without_source_links_the_headline(self):
        html = render(
            [
                section(
                    spec("Top", style="feature", show_source=False),
                    story("x", headline="Big"),
                )
            ]
        )
        assert '<h3><a href="https://ex.com/x">Big</a></h3>' in html
        assert "ExSource" not in html

    def test_brief_links_anchor_phrase_then_descriptor(self):
        html = render(
            [
                section(
                    spec("B", style="brief"),
                    story(
                        "x",
                        headline="Full headline",
                        anchor_text="OpenAI",
                        blurb="ships a thing",
                    ),
                )
            ]
        )
        assert '<a href="https://ex.com/x">OpenAI</a> — ships a thing' in html
        assert "ExSource" not in html  # brief hides source by default

    def test_quick_hit_links_headline_only(self):
        html = render(
            [
                section(
                    spec("Q", style="quick_hit"),
                    story("x", headline="Hit", emoji="⚡", blurb="ignored"),
                )
            ]
        )
        assert '<li>⚡ <a href="https://ex.com/x">Hit</a></li>' in html
        assert "ignored" not in html

    def test_link_only_uses_verbatim_source_title(self):
        html = render(
            [
                section(
                    spec("L", style="link_only", tier="roundup"),
                    story("x", headline="Written but unused", tier=Tier.ROUNDUP),
                )
            ]
        )
        assert '<a href="https://ex.com/x">x (source title)</a>' in html
        assert "Written but unused" not in html

    def test_unwritten_story_falls_back_to_source_title(self):
        html = render([section(spec("Top", style="feature"), story("x"))])
        assert "<h3>x (source title)</h3>" in html

    def test_placeholder_renders_heading_and_marker(self):
        html = render([section(SectionSpec(name="Monday Meme", type="placeholder"))])
        assert "<h2>Monday Meme</h2>" in html and "add your content here" in html

    def test_autoescape(self):
        html = render(
            [
                section(
                    spec("Q", style="quick_hit"),
                    story("x", headline='<script>alert("x")</script>'),
                )
            ]
        )
        assert "<script>" not in html and "&lt;script&gt;" in html

    def test_disclosure_and_date_present(self):
        html = render([section(spec("Q", style="quick_hit"), story("x"))])
        assert "AI-assisted." in html and "September 12, 2026" in html


class TestRendererTemplates:
    def test_custom_template_used(self, tmp_path):
        t = tmp_path / "mine.html"
        t.write_text("CUSTOM {{ newsletter_title }} {{ story_count }}")
        html = NewsletterRenderer(str(t)).render(
            issue([section(spec("Q", style="quick_hit"), story("x"))])
        )
        assert html == "CUSTOM T 1"

    def test_broken_custom_template_falls_back(self, tmp_path):
        t = tmp_path / "broken.html"
        t.write_text("{% for x in %}")
        html = NewsletterRenderer(str(t)).render(
            issue([section(spec("Q", style="quick_hit"), story("x"))])
        )
        assert "wayr-newsletter" in html  # the default rendered

    def test_missing_custom_template_falls_back(self, tmp_path):
        r = NewsletterRenderer(str(tmp_path / "nope.html"))
        assert r.custom is None
        assert "wayr-newsletter" in r.render(
            issue([section(spec("Q", style="quick_hit"), story("x"))])
        )


class TestProseFilter:
    def test_bold_and_paragraphs_but_nothing_else(self):
        from wayr.services.newsletter_renderer import prose

        out = str(prose("Fact.\n\n**Our take:** fine <b>x</b>\nnext"))
        assert (
            out
            == "Fact.<br><br><strong>Our take:</strong> fine &lt;b&gt;x&lt;/b&gt;<br>next"
        )
        assert str(prose(None)) == "" and str(prose("")) == ""


class TestReadTime:
    def test_read_minutes_floors_at_one(self):
        from wayr.utils import read_minutes

        assert read_minutes("") == 1 and read_minutes("w " * 100) == 1
        assert read_minutes("w " * 690) == 3

    def test_tag_renders_only_when_asked(self):
        from wayr.services.newsletter_renderer import NewsletterRenderer
        from wayr.models.newsletter import NewsletterIssue, NewsletterSection
        from datetime import datetime, timezone

        st = story("x", score=0.9)
        st.headline = "OpenAI ships GPT-6 Sol"
        st.read_minutes = 4
        spec_on = spec(
            "Headlines", style="feature", show_source=False, show_read_time=True
        )
        spec_off = spec("Plain", style="feature", show_source=False)
        issue = NewsletterIssue(
            title="t",
            subject_line="s",
            intro="",
            date_label="d",
            sections=[
                NewsletterSection(spec=spec_on, stories=[st]),
                NewsletterSection(spec=spec_off, stories=[st]),
            ],
            generated_at=datetime.now(timezone.utc),
            platform_name="p",
        )
        html = NewsletterRenderer().render(issue)
        assert html.count("(4 minute read)") == 1


def test_html_to_text_keeps_paragraphs_and_unescapes():
    from wayr.utils import html_to_text

    out = html_to_text(
        "<p>Pro &amp; Max first.</p><p>Second<br>line</p><script>x</script>"
    )
    assert out == "Pro & Max first.\n\nSecond\nline\n\nx" or out.startswith(
        "Pro & Max first.\n\nSecond\nline"
    )


class TestLabel:
    """An issue says what it is: the label opens the email subject and the header."""

    def _issue(self, **kw):
        from datetime import datetime, timezone

        from wayr.models.newsletter import NewsletterIssue

        base = dict(
            title="T",
            subject_line="ScreenConnect hits 47 orgs, Nvidia CEO search",
            intro="",
            date_label="November 12, 2026",
            sections=[],
            generated_at=datetime(2026, 11, 12, tzinfo=timezone.utc),
            platform_name="WAYR",
        )
        base.update(kw)
        return NewsletterIssue(**base)

    def test_email_subject_opens_with_the_label(self):
        assert (
            self._issue(label="Daily Digest").email_subject
            == "Daily Digest: ScreenConnect hits 47 orgs, Nvidia CEO search"
        )

    def test_no_label_means_the_subject_alone(self):
        assert (
            self._issue().email_subject
            == "ScreenConnect hits 47 orgs, Nvidia CEO search"
        )

    def test_label_is_not_doubled_and_title_is_the_fallback(self):
        assert (
            self._issue(
                label="Daily Digest", subject_line="Daily Digest: x"
            ).email_subject
            == "Daily Digest: x"
        )
        assert (
            self._issue(label="Daily Digest", subject_line="").email_subject
            == "Daily Digest: T"
        )

    def test_header_carries_a_kicker_only_when_labelled(self):
        from wayr.services.newsletter_renderer import NewsletterRenderer

        r = NewsletterRenderer()
        html = r.render(self._issue(label="Daily Digest"))
        assert '<p class="kicker">WAYR · Daily Digest</p>' in html
        assert "kicker" not in r.render(self._issue())
        email = r.render_email(
            self._issue(label="Daily Digest"),
            "<p>b</p>",
            web_url=None,
            unsubscribe_url="u",
        )
        assert (
            "<title>Daily Digest: ScreenConnect hits 47 orgs, Nvidia CEO search</title>"
            in email
        )
