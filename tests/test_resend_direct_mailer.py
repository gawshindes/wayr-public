"""ResendDirectMailer: our own list, Resend's batch endpoint, mocked transport."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from wayr.mailers import REGISTRY
from wayr.mailers.base import MailError
from wayr.mailers.resend_direct import (
    UNSUBSCRIBE_PLACEHOLDER,
    ResendDirectMailer,
    unsubscribe_url,
)
from wayr.models.newsletter import NewsletterIssue
from wayr.services.subscribers import PAGE, Subscriber, SubscriberList

SENDER = "WAYR Today <daily@wayr.today>"
UNSUB = "https://wayrtoday.com/newsletter/unsubscribe?token={token}"


def issue(subject="Subject here") -> NewsletterIssue:
    return NewsletterIssue(
        title="WAYR Today",
        subject_line=subject,
        intro="",
        date_label="10 Nov 2026",
        sections=[],
        generated_at=datetime(2026, 11, 10, 12, 30, tzinfo=timezone.utc),
        platform_name="WAYR",
    )


HTML = f"<p>hi</p><a href='{UNSUBSCRIBE_PLACEHOLDER}'>Unsubscribe</a>"


class FakeList:
    def __init__(self, subs):
        self.subs = subs
        self.reads = 0

    def confirmed(self):
        self.reads += 1
        return list(self.subs)

    def count(self, status="confirmed"):
        return len(self.subs)


class FakeResend:
    """Scripted batch responses: a list of (status, json|None, headers) popped per call."""

    def __init__(self, *responses, domains=(401, {"name": "restricted_api_key"})):
        self.responses = list(responses)
        self.domains = domains
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/domains":
            return httpx.Response(self.domains[0], json=self.domains[1])
        assert request.method == "POST" and request.url.path == "/emails/batch"
        if len(self.responses) > 1:
            code, body, headers = self.responses.pop(0)
        else:
            code, body, headers = self.responses[0]
        if body == "ids":
            n = len(json.loads(request.content))
            body = {"data": [{"id": f"em_{len(self.requests)}_{i}"} for i in range(n)]}
        return httpx.Response(code, json=body, headers=headers or {})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def batches(self):
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


def mailer(fake, subs=(), test_recipients=(), **kw) -> ResendDirectMailer:
    return ResendDirectMailer(
        "re_key",
        SENDER,
        UNSUB,
        FakeList(list(subs)),
        test_recipients=test_recipients,
        client=fake.client(),
        pause=0,
        **kw,
    )


SUBS = [Subscriber(f"u{i}@example.com", f"tok-{i}") for i in range(5)]


class TestConstruction:
    def test_requires_token_in_unsubscribe_url(self):
        with pytest.raises(ValueError, match="{token}"):
            ResendDirectMailer("k", SENDER, "https://x/unsub", FakeList([]))

    def test_registry(self):
        assert REGISTRY["resend-direct"] is ResendDirectMailer

    def test_unsubscribe_url(self):
        assert unsubscribe_url(UNSUB, "abc") == (
            "https://wayrtoday.com/newsletter/unsubscribe?token=abc"
        )


class TestSend:
    @pytest.mark.asyncio
    async def test_publish_sends_each_confirmed_subscriber_their_own_link(self):
        fake = FakeResend((200, "ids", None))
        m = mailer(fake, SUBS, reply_to="hello@wayr.today")
        r = await m.send(issue(), HTML, deliver=True)
        (batch,) = fake.batches()
        assert [e["to"] for e in batch] == [[s.email] for s in SUBS]
        e0 = batch[0]
        assert e0["from"] == SENDER and e0["subject"] == "Subject here"
        assert e0["reply_to"] == "hello@wayr.today"
        link = "https://wayrtoday.com/newsletter/unsubscribe?token=tok-0"
        assert link in e0["html"] and UNSUBSCRIBE_PLACEHOLDER not in e0["html"]
        assert e0["headers"] == {"List-Unsubscribe": f"<{link}>"}
        assert "attachments" not in e0
        req = fake.requests[0]
        assert req.headers["Authorization"] == "Bearer re_key"
        assert req.headers["Idempotency-Key"].startswith("wayr/20261110T123000/")
        assert (r.status, r.recipients, r.failed) == ("sent to 5", 5, 0)
        assert r.external_id == "em_1_0"
        assert m.subscribers.reads == 1  # the list is read fresh on every send

    @pytest.mark.asyncio
    async def test_batches_of_at_most_100_with_distinct_idempotency_keys(self):
        subs = [Subscriber(f"u{i}@example.com", f"t{i}") for i in range(250)]
        fake = FakeResend((200, "ids", None))
        r = await mailer(fake, subs).send(issue(), HTML, deliver=True)
        sizes = [len(b) for b in fake.batches()]
        assert sizes == [100, 100, 50]
        keys = [q.headers["Idempotency-Key"] for q in fake.requests]
        assert len(set(keys)) == 3
        assert r.recipients == 250

    @pytest.mark.asyncio
    async def test_draft_goes_to_test_recipients_only(self):
        fake = FakeResend((200, "ids", None))
        m = mailer(fake, SUBS, test_recipients=["me@wayr.today"])
        r = await m.send(issue(), HTML, deliver=False)
        (batch,) = fake.batches()
        assert [e["to"] for e in batch] == [["me@wayr.today"]]
        assert r.status == "test send to 1"
        assert m.subscribers.reads == 0

    @pytest.mark.asyncio
    async def test_draft_without_test_recipients_is_an_error(self):
        with pytest.raises(MailError, match="NEWSLETTER_TEST_RECIPIENTS"):
            await mailer(FakeResend((200, "ids", None)), SUBS).send(issue(), HTML)

    @pytest.mark.asyncio
    async def test_no_confirmed_subscribers_sends_nothing(self):
        fake = FakeResend((200, "ids", None))
        r = await mailer(fake, []).send(issue(), HTML, deliver=True)
        assert fake.batches() == []
        assert (r.status, r.recipients) == ("no recipients", 0)

    @pytest.mark.asyncio
    async def test_refuses_html_without_placeholder(self):
        with pytest.raises(MailError, match="unsubscribe"):
            await mailer(FakeResend((200, "ids", None)), SUBS).send(
                issue(), "<p>x</p>", deliver=True
            )

    @pytest.mark.asyncio
    async def test_429_then_success_retries_same_key(self):
        fake = FakeResend(
            (429, {"name": "rate_limit_exceeded"}, {"retry-after": "0"}),
            (200, "ids", None),
        )
        r = await mailer(fake, SUBS, attempts=3).send(issue(), HTML, deliver=True)
        keys = [q.headers["Idempotency-Key"] for q in fake.requests]
        assert len(keys) == 2 and keys[0] == keys[1]
        assert r.recipients == 5

    @pytest.mark.asyncio
    async def test_rejected_batch_is_counted_not_fatal_when_others_succeed(self):
        subs = [Subscriber(f"u{i}@example.com", f"t{i}") for i in range(150)]
        fake = FakeResend(
            (422, {"name": "validation_error", "message": "bad"}, None),
            (200, "ids", None),
        )
        r = await mailer(fake, subs).send(issue(), HTML, deliver=True)
        assert (r.recipients, r.failed) == (50, 100)
        assert r.status == "sent to 50 (100 failed)"

    @pytest.mark.asyncio
    async def test_all_batches_failing_raises(self):
        fake = FakeResend((500, {"message": "down"}, None))
        with pytest.raises(MailError, match="every batch failed"):
            await mailer(fake, SUBS, attempts=2).send(issue(), HTML, deliver=True)


class TestConnection:
    @pytest.mark.asyncio
    async def test_sending_only_key_passes_with_reachable_table(self):
        assert await mailer(FakeResend((200, "ids", None)), SUBS).test_connection()

    @pytest.mark.asyncio
    async def test_full_key_checks_domain(self):
        ok = FakeResend(
            (200, "ids", None),
            domains=(200, {"data": [{"name": "wayr.today", "status": "verified"}]}),
        )
        assert await mailer(ok, SUBS).test_connection() is True
        bad = FakeResend(
            (200, "ids", None),
            domains=(200, {"data": [{"name": "wayrtoday.com", "status": "verified"}]}),
        )
        assert await mailer(bad, SUBS).test_connection() is False

    @pytest.mark.asyncio
    async def test_unreachable_table_fails(self):
        class Broken:
            def count(self, status="confirmed"):
                raise RuntimeError("no table")

        fake = FakeResend((200, "ids", None))
        m = ResendDirectMailer("k", SENDER, UNSUB, Broken(), client=fake.client())
        assert await m.test_connection() is False


class TestSubscriberList:
    class FakeQuery:
        def __init__(self, rows):
            self.rows, self.start, self.end = rows, 0, 0
            self.filters = {}

        def select(self, *a, **k):
            return self

        def eq(self, col, val):
            self.filters[col] = val
            return self

        def order(self, *a, **k):
            return self

        def range(self, start, end):
            self.start, self.end = start, end
            return self

        def limit(self, n):
            return self

        def execute(self):
            class R:
                pass

            r = R()
            r.data = self.rows[self.start : self.end + 1]
            r.count = len(self.rows)
            return r

    class FakeClient:
        def __init__(self, rows):
            self.rows = rows
            self.queries = []

        def table(self, name):
            q = TestSubscriberList.FakeQuery(self.rows)
            self.queries.append(q)
            return q

    def test_pages_through_confirmed_rows_and_skips_incomplete(self):
        rows = [
            {"email": f"U{i}@Example.com ", "unsubscribe_token": f"t{i}"}
            for i in range(PAGE + 3)
        ]
        rows.append({"email": "x@example.com", "unsubscribe_token": None})
        client = self.FakeClient(rows)
        subs = SubscriberList("https://x", "key", client=client).confirmed()
        assert len(subs) == PAGE + 3
        assert subs[0] == Subscriber("u0@example.com", "t0")
        assert all(q.filters == {"status": "confirmed"} for q in client.queries)
        assert len(client.queries) == 2  # two pages

    def test_count(self):
        client = self.FakeClient([{"email": "a"}, {"email": "b"}])
        assert SubscriberList("https://x", "key", client=client).count() == 2

    def test_requires_credentials(self):
        with pytest.raises(ValueError):
            SubscriberList("", "")
