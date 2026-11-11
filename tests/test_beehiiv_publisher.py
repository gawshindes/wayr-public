"""Tests for BeehiivPublisher against a mocked HTTP transport (no network)."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from wayr.models.newsletter import NewsletterIssue
from wayr.publishers import REGISTRY, get_newsletter_publisher
from wayr.publishers.base import PublishError
from wayr.publishers.beehiiv import BeehiivPublisher

PUB = "pub_123"


def issue(subject="Subject here", title="Title here") -> NewsletterIssue:
    return NewsletterIssue(
        title=title,
        subject_line=subject,
        intro="",
        date_label="d",
        sections=[],
        generated_at=datetime(2026, 9, 15, tzinfo=timezone.utc),
        platform_name="WAYR",
    )


class FakeBeehiiv:
    """Scripted responses: `gets` is the sequence of (status, json) for GET post polls."""

    def __init__(
        self,
        create=(201, {"data": {"id": "post_abc"}}),
        gets=(
            (
                200,
                {
                    "data": {
                        "id": "post_abc",
                        "status": "draft",
                        "web_url": "https://x.beehiiv.com/p/abc",
                    }
                },
            ),
        ),
        publication=(200, {"data": {"id": PUB}}),
    ):
        self.create, self.gets, self.publication = create, list(gets), publication
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith(f"/publications/{PUB}/posts"):
            code, body = self.create
        elif request.method == "GET" and f"/publications/{PUB}/posts/" in path:
            code, body = self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]
        elif request.method == "GET" and path.endswith(f"/publications/{PUB}"):
            code, body = self.publication
        else:
            return httpx.Response(404, json={"error": "unknown route"})
        return httpx.Response(code, json=body)


def publisher(fake: FakeBeehiiv, **kw) -> BeehiivPublisher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return BeehiivPublisher("key", PUB, poll_interval=0, client=client, **kw)


class TestPublish:
    @pytest.mark.asyncio
    async def test_creates_draft_with_subject_as_title_and_confirms(self):
        fake = FakeBeehiiv()
        r = await publisher(fake).publish(issue(), "<p>hi</p>")
        assert (r.publisher, r.external_id, r.status, r.url) == (
            "beehiiv",
            "post_abc",
            "draft",
            "https://x.beehiiv.com/p/abc",
        )
        post = fake.requests[0]
        assert post.headers["authorization"] == "Bearer key"
        assert json.loads(post.content) == {
            "title": "Subject here",
            "body_content": "<p>hi</p>",
        }
        assert "status" not in json.loads(post.content)  # create has no status field

    @pytest.mark.asyncio
    async def test_falls_back_to_title_when_subject_blank(self):
        fake = FakeBeehiiv()
        await publisher(fake).publish(issue(subject="  "), "<p/>")
        assert json.loads(fake.requests[0].content)["title"] == "Title here"

    @pytest.mark.asyncio
    async def test_polls_through_202_until_200(self):
        fake = FakeBeehiiv(
            gets=[
                (202, {}),
                (202, {}),
                (200, {"data": {"status": "draft", "web_url": "u"}}),
            ]
        )
        r = await publisher(fake).publish(issue(), "<p/>")
        assert r.status == "draft" and r.url == "u"
        assert sum(1 for q in fake.requests if q.method == "GET") == 3

    @pytest.mark.asyncio
    async def test_creation_failed_is_a_publish_error(self):
        fake = FakeBeehiiv(
            gets=[(404, {"code": "POST_CREATION_FAILED", "message": "bad html"})]
        )
        with pytest.raises(PublishError, match="creation failed"):
            await publisher(fake).publish(issue(), "<p/>")

    @pytest.mark.asyncio
    async def test_still_processing_after_polls_is_accepted_not_failed(self):
        fake = FakeBeehiiv(gets=[(202, {})])
        r = await publisher(fake, poll_attempts=3).publish(issue(), "<p/>")
        assert r.status == "accepted" and r.url is None and r.external_id == "post_abc"

    @pytest.mark.asyncio
    async def test_create_4xx_raises_with_body(self):
        fake = FakeBeehiiv(create=(401, {"message": "invalid token"}))
        with pytest.raises(PublishError, match="401.*invalid token"):
            await publisher(fake).publish(issue(), "<p/>")

    @pytest.mark.asyncio
    async def test_create_without_id_raises(self):
        fake = FakeBeehiiv(create=(201, {"data": {}}))
        with pytest.raises(PublishError, match="no post id"):
            await publisher(fake).publish(issue(), "<p/>")

    @pytest.mark.asyncio
    async def test_transport_error_raises_publish_error(self):
        def boom(request):
            raise httpx.ConnectError("refused")

        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        with pytest.raises(PublishError, match="refused"):
            await BeehiivPublisher("k", PUB, poll_interval=0, client=client).publish(
                issue(), "<p/>"
            )


class TestConnection:
    @pytest.mark.asyncio
    async def test_ok(self):
        assert await publisher(FakeBeehiiv()).test_connection() is True

    @pytest.mark.asyncio
    async def test_unauthorized(self):
        assert (
            await publisher(FakeBeehiiv(publication=(401, {}))).test_connection()
            is False
        )


class TestRegistry:
    def test_beehiiv_registered(self):
        assert REGISTRY["beehiiv"] is BeehiivPublisher

    def test_unknown_name_fails_fast(self):
        class S:
            newsletter_publisher = "ghost"

        with pytest.raises(ValueError, match="Unknown NEWSLETTER_PUBLISHER 'ghost'"):
            get_newsletter_publisher(S())

    def test_from_settings_requires_credentials(self):
        class S:
            newsletter_publisher = "beehiiv"
            beehiiv_api_key = ""
            beehiiv_publication_id = "p"

        with pytest.raises(ValueError, match="needs api_key"):
            get_newsletter_publisher(S())
