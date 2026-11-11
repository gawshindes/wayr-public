"""WordPressNewsletterPublisher against a mocked WordPressClient."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from wayr.models.newsletter import NewsletterIssue
from wayr.publishers import REGISTRY, get_newsletter_publisher
from wayr.publishers.base import PublishError
from wayr.publishers.wordpress import WordPressNewsletterPublisher


def issue(
    subject="Subject here", title="Title here", intro="Intro para"
) -> NewsletterIssue:
    return NewsletterIssue(
        title=title,
        subject_line=subject,
        intro=intro,
        date_label="d",
        sections=[],
        generated_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        platform_name="WAYR",
    )


def publisher(client=None, **kw) -> WordPressNewsletterPublisher:
    c = client or MagicMock()
    c.get_or_create_category.return_value = 42
    c.get_or_create_tag.side_effect = lambda name: {"newsletter": 7, "daily": 8}[name]
    c.create_post.return_value = {
        "id": 1234,
        "link": "https://cms.example/?p=1234",
        "status": "draft",
    }
    c.test_connection.return_value = True
    return WordPressNewsletterPublisher("https://cms.example", "u", "p", client=c, **kw)


class TestPublish:
    @pytest.mark.asyncio
    async def test_creates_draft_in_category_with_intro_as_excerpt(self):
        p = publisher(category="Weekly")
        r = await p.publish(issue(), "<p>hi</p>")
        assert (r.publisher, r.external_id, r.url, r.status) == (
            "wordpress",
            "1234",
            "https://cms.example/?p=1234",
            "draft",
        )
        p.client.get_or_create_category.assert_called_once_with("Weekly")
        p.client.create_post.assert_called_once_with(
            title="Subject here",
            content="<p>hi</p>",
            status="draft",
            categories=[42],
            tags=None,
            excerpt="Intro para",
        )

    @pytest.mark.asyncio
    async def test_never_publishes(self):
        p = publisher()
        await p.publish(issue(), "<p/>")
        assert p.client.create_post.call_args.kwargs["status"] == "draft"

    @pytest.mark.asyncio
    async def test_blank_subject_falls_back_to_title(self):
        p = publisher()
        await p.publish(issue(subject=" "), "<p/>")
        assert p.client.create_post.call_args.kwargs["title"] == "Title here"

    @pytest.mark.asyncio
    async def test_client_error_is_publish_error(self):
        p = publisher()
        p.client.create_post.side_effect = RuntimeError("401 Unauthorized")
        with pytest.raises(PublishError, match="401 Unauthorized"):
            await p.publish(issue(), "<p/>")

    @pytest.mark.asyncio
    async def test_missing_id_is_publish_error(self):
        p = publisher()
        p.client.create_post.return_value = {"link": "x"}
        with pytest.raises(PublishError, match="no post id"):
            await p.publish(issue(), "<p/>")


class TestConnectionAndRegistry:
    @pytest.mark.asyncio
    async def test_connection_delegates(self):
        p = publisher()
        assert await p.test_connection() is True
        p.client.test_connection.return_value = False
        assert await p.test_connection() is False

    def test_registered(self):
        assert REGISTRY["wordpress"] is WordPressNewsletterPublisher

    def test_from_settings_requires_credentials(self):
        class S:
            newsletter_publisher = "wordpress"
            wordpress_url = None
            wordpress_username = "u"
            wordpress_app_password = "p"
            newsletter_wordpress_category = "N"
            wp_taxonomy_cache_file = "x.json"

        with pytest.raises(ValueError, match="needs site_url"):
            get_newsletter_publisher(S())


class TestDeliveryAndHeading:
    @pytest.mark.asyncio
    async def test_template_h1_is_stripped_because_wordpress_shows_the_title(self):
        p = publisher()
        html = '<h1 class="t">Issue title</h1>\n<p class="date">Sep 24</p><h2>Top</h2>'
        await p.publish(issue(), html)
        sent = p.client.create_post.call_args.kwargs["content"]
        assert "<h1" not in sent and sent.startswith('<p class="date">')

    @pytest.mark.asyncio
    async def test_publish_delivery_publishes(self):
        p = publisher()
        p.delivery = "publish"
        await p.publish(issue(), "<p/>")
        assert p.client.create_post.call_args.kwargs["status"] == "publish"

    def test_unknown_delivery_is_loud(self):
        from unittest.mock import MagicMock

        from wayr.publishers.wordpress import WordPressNewsletterPublisher

        with pytest.raises(ValueError, match="delivery"):
            WordPressNewsletterPublisher(
                "https://s", "u", "p", client=MagicMock(), delivery="send"
            )

    def test_settings_default_is_draft(self):
        from wayr.config import Settings

        assert Settings.model_fields["newsletter_delivery"].default == "draft"


class TestTags:
    @pytest.mark.asyncio
    async def test_default_tag_is_newsletter(self):
        p = publisher(tags=["newsletter"])
        await p.publish(issue(), "<p>hi</p>")
        assert p.client.create_post.call_args.kwargs["tags"] == [7]
        p.client.get_or_create_tag.assert_called_once_with("newsletter")

    @pytest.mark.asyncio
    async def test_several_tags_blank_entries_dropped(self):
        p = publisher(tags=["newsletter", " ", "daily"])
        await p.publish(issue(), "<p>hi</p>")
        assert p.client.create_post.call_args.kwargs["tags"] == [7, 8]

    @pytest.mark.asyncio
    async def test_no_tags_sends_none(self):
        p = publisher(tags=[])
        await p.publish(issue(), "<p>hi</p>")
        assert p.client.create_post.call_args.kwargs["tags"] is None
