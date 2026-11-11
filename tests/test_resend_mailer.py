"""ResendMailer against a mocked HTTP transport (no network)."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from wayr.mailers import NONE, REGISTRY, get_newsletter_mailer
from wayr.mailers.base import MailError
from wayr.mailers.resend import UNSUBSCRIBE_PLACEHOLDER, ResendMailer, sender_domain
from wayr.models.newsletter import NewsletterIssue
from wayr.services.newsletter_renderer import NewsletterRenderer

SEG = "seg_123"
SENDER = "WAYR Today <daily@wayrtoday.com>"


def issue(subject="Subject here", title="WAYR Today") -> NewsletterIssue:
    return NewsletterIssue(
        title=title,
        subject_line=subject,
        intro="",
        date_label="10 Nov 2026",
        sections=[],
        generated_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
        platform_name="WAYR",
    )


HTML = f"<p>hi</p><a href='{UNSUBSCRIBE_PLACEHOLDER}'>Unsubscribe</a>"


class FakeResend:
    def __init__(
        self,
        create=(201, {"object": "broadcast", "id": "bc_1"}),
        get=(200, {"object": "broadcast", "id": "bc_1", "status": "queued"}),
        domains=(200, {"data": [{"name": "wayrtoday.com", "status": "verified"}]}),
        segment=(200, {"object": "segment", "id": SEG, "name": "WAYR Today"}),
    ):
        self.create, self.get, self.domains, self.segment = (
            create,
            get,
            domains,
            segment,
        )
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, m = request.url.path, request.method
        if m == "POST" and path == "/broadcasts":
            code, body = self.create
        elif m == "GET" and path.startswith("/broadcasts/"):
            code, body = self.get
        elif m == "GET" and path == "/domains":
            code, body = self.domains
        elif m == "GET" and path == f"/segments/{SEG}":
            code, body = self.segment
        else:
            code, body = 404, {"message": "no route"}
        return httpx.Response(code, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def posted(self) -> dict:
        return json.loads(self.requests[0].content)


def mailer(fake: FakeResend, **kw) -> ResendMailer:
    return ResendMailer("re_key", SEG, SENDER, client=fake.client(), **kw)


class TestSenderDomain:
    def test_named_and_bare_addresses(self):
        assert sender_domain(SENDER) == "wayrtoday.com"
        assert sender_domain("daily@Example.COM") == "example.com"
        assert sender_domain("not an address") is None

    def test_constructor_rejects_bad_sender_and_missing_fields(self):
        with pytest.raises(ValueError, match="NEWSLETTER_FROM"):
            ResendMailer("k", SEG, "WAYR Today")
        with pytest.raises(ValueError):
            ResendMailer("", SEG, SENDER)


class TestSend:
    @pytest.mark.asyncio
    async def test_draft_by_default_subject_from_subject_line(self):
        fake = FakeResend(get=(200, {"id": "bc_1", "status": "draft"}))
        r = await mailer(fake, reply_to="hello@wayrtoday.com").send(issue(), HTML)
        body = fake.posted()
        assert body["segment_id"] == SEG
        assert body["from"] == SENDER
        assert body["subject"] == "Subject here"
        assert body["html"] == HTML
        assert body["reply_to"] == "hello@wayrtoday.com"
        assert "send" not in body
        assert fake.requests[0].headers["Authorization"] == "Bearer re_key"
        assert (r.mailer, r.external_id, r.status) == ("resend", "bc_1", "draft")
        assert r.url.endswith("/broadcasts/bc_1")

    @pytest.mark.asyncio
    async def test_deliver_sets_send_true_and_reads_status(self):
        fake = FakeResend()
        r = await mailer(fake).send(issue(), HTML, deliver=True)
        assert fake.posted()["send"] is True
        assert r.status == "queued"
        assert [q.method for q in fake.requests] == ["POST", "GET"]

    @pytest.mark.asyncio
    async def test_deliver_but_still_draft_is_an_error(self):
        fake = FakeResend(get=(200, {"id": "bc_1", "status": "draft"}))
        with pytest.raises(MailError, match="'draft' after a send request"):
            await mailer(fake).send(issue(), HTML, deliver=True)

    @pytest.mark.asyncio
    async def test_status_read_failure_falls_back_to_requested_state(self):
        fake = FakeResend(get=(500, {"message": "boom"}))
        r = await mailer(fake).send(issue(), HTML, deliver=True)
        assert r.status == "queued"
        r = await mailer(fake).send(issue(), HTML)
        assert r.status == "draft"

    @pytest.mark.asyncio
    async def test_refuses_html_without_unsubscribe_link(self):
        fake = FakeResend()
        with pytest.raises(MailError, match="unsubscribe"):
            await mailer(fake).send(issue(), "<p>no footer</p>")
        assert fake.requests == []

    @pytest.mark.asyncio
    async def test_create_errors_surface(self):
        with pytest.raises(MailError, match="403"):
            await mailer(FakeResend(create=(403, {"message": "quota"}))).send(
                issue(), HTML
            )
        with pytest.raises(MailError, match="no broadcast id"):
            await mailer(FakeResend(create=(201, {"object": "broadcast"}))).send(
                issue(), HTML
            )

    @pytest.mark.asyncio
    async def test_subject_falls_back_to_title(self):
        fake = FakeResend()
        await mailer(fake).send(issue(subject=""), HTML)
        assert fake.posted()["subject"] == "WAYR Today"


class TestConnection:
    @pytest.mark.asyncio
    async def test_ok_when_domain_verified_and_segment_exists(self):
        assert await mailer(FakeResend()).test_connection() is True

    @pytest.mark.asyncio
    async def test_unverified_domain_fails(self):
        fake = FakeResend(
            domains=(200, {"data": [{"name": "wayrtoday.com", "status": "pending"}]})
        )
        assert await mailer(fake).test_connection() is False

    @pytest.mark.asyncio
    async def test_missing_segment_or_bad_key_fails(self):
        assert await mailer(FakeResend(segment=(404, {}))).test_connection() is False
        assert await mailer(FakeResend(domains=(401, {}))).test_connection() is False


class TestRegistry:
    def test_registry_and_none(self):
        assert REGISTRY["resend"] is ResendMailer

        class S:
            newsletter_mailer = NONE

        assert get_newsletter_mailer(S()) is None

        class Bad:
            newsletter_mailer = "mailchimp"

        with pytest.raises(ValueError, match="Unknown NEWSLETTER_MAILER"):
            get_newsletter_mailer(Bad())

    def test_from_settings(self):
        class S:
            newsletter_mailer = "resend"
            resend_api_key = "re_k"
            resend_segment_id = SEG
            newsletter_from = SENDER
            newsletter_reply_to = None
            resend_api_base = "https://mock.local/"

        m = get_newsletter_mailer(S())
        assert isinstance(m, ResendMailer)
        assert m.api_base == "https://mock.local"


class TestEmailTemplate:
    def test_wraps_body_with_web_link_and_unsubscribe(self):
        r = NewsletterRenderer()
        body = r.render(issue())
        html = r.render_email(
            issue(),
            body,
            web_url="https://wayrtoday.com/newsletter/1",
            unsubscribe_url=UNSUBSCRIBE_PLACEHOLDER,
        )
        assert html.startswith("<!doctype html>")
        assert body.strip() in html  # the issue HTML travels verbatim, not re-escaped
        assert 'href="https://wayrtoday.com/newsletter/1"' in html
        assert f'href="{UNSUBSCRIBE_PLACEHOLDER}"' in html
        assert "<title>Subject here</title>" in html

    def test_no_web_url_omits_the_link(self):
        r = NewsletterRenderer()
        html = r.render_email(issue(), "<p>x</p>", web_url=None, unsubscribe_url="#u")
        assert "Read this issue on the web" not in html
        assert 'href="#u"' in html
