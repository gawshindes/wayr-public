"""The issue's social copy, and SocialMediaAgent.post_link against mocked platform calls."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from wayr.agents.social_media import SocialMediaAgent
from wayr.models.newsletter import (
    NewsletterIssue,
    NewsletterSection,
    NewsletterStory,
    SectionSpec,
)
from wayr.services.newsletter_social import social_copy


def story(headline, blurb="", url="https://src.example/a"):
    return NewsletterStory(
        url=url,
        url_hash="h" + headline[:6],
        source="Src",
        source_title=headline,
        headline=headline,
        blurb=blurb,
        category="AI",
        tier="feature",
        importance=0.9,
        published_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
    )


def issue(intro="**Anthropic** filed to go public.\n\nHere is the day.", sections=None):
    if sections is None:
        sections = [
            NewsletterSection(
                name="Top Story",
                spec=SectionSpec(
                    name="Top Story",
                    type="stories",
                    style="feature",
                    tier="feature",
                    max=1,
                ),
                stories=[
                    story(
                        "Anthropic opens IPO roadshow", blurb="The lab filed on Monday."
                    )
                ],
            ),
            NewsletterSection(
                name="Also",
                spec=SectionSpec(
                    name="Also",
                    type="stories",
                    style="link_only",
                    tier="roundup",
                    max=5,
                ),
                stories=[story("A link-only item")],
            ),
        ]
    return NewsletterIssue(
        title="WAYR Today",
        subject_line="Anthropic's $1 trillion roadshow",
        intro=intro,
        date_label="10 Nov 2026",
        sections=sections,
        generated_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
        platform_name="WAYR",
    )


class TestSocialCopy:
    def test_intro_and_feature_headlines_without_markdown(self):
        c = social_copy(issue())
        assert c.title == "Anthropic's $1 trillion roadshow"
        assert c.linkedin.startswith("Anthropic filed to go public.")
        assert "**" not in c.linkedin
        assert "In today's issue:\n• Anthropic opens IPO roadshow" in c.linkedin
        assert "A link-only item" not in c.linkedin  # roundup links are not headlines
        assert c.tweet == "Anthropic opens IPO roadshow"
        assert c.description.startswith("Anthropic filed to go public.")

    def test_no_intro_falls_back_to_blurb_and_title(self):
        c = social_copy(issue(intro=""))
        assert c.description == "The lab filed on Monday."
        assert c.linkedin.startswith("In today's issue:")
        c = social_copy(issue(intro="", sections=[]))
        assert (c.linkedin, c.tweet, c.description) == (c.title, c.title, c.title)


def agent(tmp_path, twitter=True, linkedin=True) -> SocialMediaAgent:
    kw = {}
    if twitter:
        kw.update(
            twitter_api_key="k",
            twitter_api_secret="s",
            twitter_access_token="t",
            twitter_access_token_secret="ts",
        )
    if linkedin:
        kw.update(
            linkedin_access_token="lt",
            linkedin_organization_urn="urn:li:organization:1",
        )
    a = SocialMediaAgent(
        tracking_file=str(tmp_path / "twitter_posted.json"),
        linkedin_tracking_file=str(tmp_path / "linkedin_posted.json"),
        **kw,
    )
    a._post_tweet = AsyncMock(return_value={"success": True, "tweet_id": "tw1"})
    a._post_linkedin_article = AsyncMock(
        return_value={"success": True, "urn": "urn:li:share:1"}
    )
    return a


URL = "https://wayrtoday.com/newsletter/2026-11-10"


class TestPostLink:
    @pytest.mark.asyncio
    async def test_posts_to_both_and_records_the_url(self, tmp_path):
        a = agent(tmp_path)
        r = await a.post_link(URL, "Title", "LinkedIn text", "Tweet text", "desc")
        assert r["twitter"] == {"posted": 1, "failed": 0, "skipped": 0, "id": "tw1"}
        assert r["linkedin"] == {
            "posted": 1,
            "failed": 0,
            "skipped": 0,
            "id": "urn:li:share:1",
        }
        tweet = a._post_tweet.call_args.args[0]
        assert tweet == f"Tweet text\n\n{URL}"
        li = a._post_linkedin_article.call_args.kwargs
        assert (
            li["commentary"],
            li["article_url"],
            li["article_title"],
            li["article_description"],
        ) == (
            "LinkedIn text",
            URL,
            "Title",
            "desc",
        )
        assert (tmp_path / "linkedin_posted.json").exists()

    @pytest.mark.asyncio
    async def test_second_run_is_skipped(self, tmp_path):
        a = agent(tmp_path)
        await a.post_link(URL, "T", "L", "W")
        r = await a.post_link(URL, "T", "L", "W")
        assert r["twitter"]["skipped"] == 1 and r["linkedin"]["skipped"] == 1
        assert (
            a._post_tweet.await_count == 1 and a._post_linkedin_article.await_count == 1
        )

    @pytest.mark.asyncio
    async def test_failure_is_counted_and_not_recorded(self, tmp_path):
        a = agent(tmp_path, twitter=False)
        a._post_linkedin_article = AsyncMock(
            return_value={"success": False, "error": "401"}
        )
        r = await a.post_link(URL, "T", "L", "W")
        assert r["twitter"]["skipped"] == 1
        assert r["linkedin"]["failed"] == 1 and r["linkedin"]["error"] == "401"
        assert not (tmp_path / "linkedin_posted.json").exists()
