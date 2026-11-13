"""Resend direct mailer: the issue to our own list, one email per subscriber.

Where `resend` sends one Broadcast to a Segment that lives at Resend, this one
keeps the list where the site keeps it — the `subscribers` table in Supabase
(see wayr/services/subscribers.py) — and uses Resend only as the transport:

- `POST /emails/batch`: up to 100 emails per request, each with its own `to`,
  `html` and `headers`; the response is `{"data": [{"id": ...}, ...]}` in payload
  order. `attachments` is the one field batch does not support (we send none).
- Each email's HTML gets that subscriber's own unsubscribe link, from
  NEWSLETTER_UNSUBSCRIBE_URL with `{token}` replaced by the row's
  `unsubscribe_token` — the link the site's /newsletter/unsubscribe page
  expects — in the footer and in a `List-Unsubscribe` header.
- `Idempotency-Key` (kept 24h) on every batch, so a retry after a timeout never
  sends the same batch twice. A 409 `concurrent_idempotent_requests` is retried.
- Rate limit 10 requests/second per team: a 10,000-subscriber issue is 100
  requests; we pace them and honour `retry-after` on 429.
- A "Sending access" key is enough: this adapter only ever POSTs emails.

Delivery: `publish` sends to every confirmed subscriber; `draft` sends the
issue to NEWSLETTER_TEST_RECIPIENTS only (the operator's inbox), so a draft is
something you can read in a mail client, not a row in a dashboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, Dict, List, Optional, Sequence

import httpx

from wayr.mailers.base import MailError, MailResult, NewsletterMailer
from wayr.mailers.resend import DEFAULT_API_BASE, sender_domain
from wayr.models.newsletter import NewsletterIssue
from wayr.services.subscribers import Subscriber, SubscriberList
from wayr.utils import get_logger

logger = get_logger(__name__)

UNSUBSCRIBE_PLACEHOLDER = "{{{WAYR_UNSUBSCRIBE_URL}}}"
BATCH = 100
_KEY_SAFE = re.compile(r"[^A-Za-z0-9_./-]+")


def unsubscribe_url(template: str, token: str) -> str:
    return template.replace("{token}", token)


class ResendDirectMailer(NewsletterMailer):
    name = "resend-direct"
    unsubscribe_placeholder = UNSUBSCRIBE_PLACEHOLDER

    def __init__(
        self,
        api_key: str,
        sender: str,
        unsubscribe_template: str,
        subscribers: SubscriberList,
        reply_to: Optional[str] = None,
        test_recipients: Sequence[str] = (),
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 60.0,
        batch_size: int = BATCH,
        attempts: int = 4,
        pause: float = 0.15,  # between batches: well under 10 req/s
        client: Optional[httpx.AsyncClient] = None,
    ):
        if not api_key or not sender:
            raise ValueError("ResendDirectMailer needs api_key and sender")
        if sender_domain(sender) is None:
            raise ValueError(
                f"NEWSLETTER_FROM must be an address or 'Name <address>', not {sender!r}"
            )
        if "{token}" not in (unsubscribe_template or ""):
            raise ValueError(
                "NEWSLETTER_UNSUBSCRIBE_URL must contain {token}, e.g. "
                "https://example.com/newsletter/unsubscribe?token={token}"
            )
        self.api_key = api_key
        self.sender = sender.strip()
        self.unsubscribe_template = unsubscribe_template.strip()
        self.subscribers = subscribers
        self.reply_to = (reply_to or "").strip() or None
        self.test_recipients = [r.strip() for r in test_recipients if r and r.strip()]
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.batch_size = max(1, min(batch_size, BATCH))
        self.attempts = attempts
        self.pause = pause
        self._client = client

    @classmethod
    def from_settings(cls, settings) -> "ResendDirectMailer":
        return cls(
            api_key=settings.resend_api_key or "",
            sender=settings.newsletter_from or "",
            unsubscribe_template=settings.newsletter_unsubscribe_url or "",
            subscribers=SubscriberList(
                settings.supabase_url or "", settings.supabase_secret_key or ""
            ),
            reply_to=getattr(settings, "newsletter_reply_to", None),
            test_recipients=settings.newsletter_test_recipient_list,
            api_base=getattr(settings, "resend_api_base", DEFAULT_API_BASE),
        )

    # ---- http ----

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _post(
        self, path: str, body: Any, headers: Dict[str, str]
    ) -> httpx.Response:
        url = f"{self.api_base}{path}"
        hdrs = {**self._headers, **headers}
        if self._client is not None:
            return await self._client.post(url, json=body, headers=hdrs)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.post(url, json=body, headers=hdrs)

    @staticmethod
    def _snippet(resp: httpx.Response) -> str:
        return resp.text[:300].replace("\n", " ")

    # ---- building ----

    def _email(
        self, issue: NewsletterIssue, html: str, sub: Subscriber
    ) -> Dict[str, Any]:
        link = unsubscribe_url(self.unsubscribe_template, sub.token)
        msg: Dict[str, Any] = {
            "from": self.sender,
            "to": [sub.email],
            "subject": issue.email_subject,
            "html": html.replace(UNSUBSCRIBE_PLACEHOLDER, link),
            "headers": {"List-Unsubscribe": f"<{link}>"},
        }
        if self.reply_to:
            msg["reply_to"] = self.reply_to
        return msg

    @staticmethod
    def _idempotency_key(issue: NewsletterIssue, index: int) -> str:
        stamp = issue.generated_at.strftime("%Y%m%dT%H%M%S")
        digest = hashlib.sha1(issue.email_subject.encode("utf-8")).hexdigest()[:10]
        return _KEY_SAFE.sub("-", f"wayr/{stamp}/{digest}/{index}")[:256]

    async def _send_batch(
        self, issue: NewsletterIssue, batch: List[Dict[str, Any]], index: int
    ) -> List[str]:
        """Send one batch with retries. Returns the ids; raises MailError when spent."""
        key = self._idempotency_key(issue, index)
        last = ""
        for attempt in range(1, self.attempts + 1):
            try:
                resp = await self._post(
                    "/emails/batch", batch, {"Idempotency-Key": key}
                )
            except httpx.HTTPError as e:
                last = f"network error: {e}"
                resp = None
            if resp is not None:
                if resp.status_code in (200, 201):
                    try:
                        data = (resp.json() or {}).get("data") or []
                    except ValueError:
                        data = []
                    ids = [str(d.get("id") or "") for d in data if isinstance(d, dict)]
                    if len(ids) != len(batch):
                        logger.warning(
                            f"Resend batch {index}: {len(ids)} ids for {len(batch)} emails"
                        )
                    return ids or ["accepted"] * len(batch)
                last = f"{resp.status_code}: {self._snippet(resp)}"
                retryable = (
                    resp.status_code in (408, 409, 429) or resp.status_code >= 500
                )
                if not retryable:
                    raise MailError(f"Resend batch {index} rejected ({last})")
                wait = float(resp.headers.get("retry-after") or 0) or 2.0 * attempt
            else:
                wait = 2.0 * attempt
            if attempt < self.attempts:
                logger.warning(
                    f"Resend batch {index}: attempt {attempt} failed ({last}); "
                    f"retrying in {wait:.0f}s"
                )
                await asyncio.sleep(wait)
        raise MailError(
            f"Resend batch {index} failed after {self.attempts} attempts ({last})"
        )

    # ---- contract ----

    async def send(
        self, issue: NewsletterIssue, html: str, *, deliver: bool = False
    ) -> MailResult:
        if UNSUBSCRIBE_PLACEHOLDER not in html:
            raise MailError(
                "email HTML has no unsubscribe placeholder; render it with the email template"
            )
        if deliver:
            recipients = await asyncio.to_thread(self.subscribers.confirmed)
            audience = "confirmed subscribers"
        else:
            if not self.test_recipients:
                raise MailError(
                    "NEWSLETTER_DELIVERY=draft sends the issue to NEWSLETTER_TEST_RECIPIENTS, "
                    "which is empty"
                )
            recipients = [
                Subscriber(email=e, token="test") for e in self.test_recipients
            ]
            audience = "test recipients"
        if not recipients:
            logger.warning(f"Resend: no {audience}; nothing sent")
            return MailResult(
                mailer=self.name,
                external_id="none",
                status="no recipients",
                recipients=0,
            )

        batches = [
            recipients[i : i + self.batch_size]
            for i in range(0, len(recipients), self.batch_size)
        ]
        logger.info(
            f"Resend: sending '{issue.email_subject[:70]}' to "
            f"{len(recipients)} {audience} in {len(batches)} batch(es)"
        )
        ids: List[str] = []
        failed = 0
        errors: List[str] = []
        for index, group in enumerate(batches):
            payload = [self._email(issue, html, s) for s in group]
            try:
                ids.extend(await self._send_batch(issue, payload, index))
            except MailError as e:
                failed += len(group)
                errors.append(str(e))
                logger.error(str(e))
            if index + 1 < len(batches):
                await asyncio.sleep(self.pause)

        sent = len(recipients) - failed
        if sent == 0:
            raise MailError(
                f"Resend: every batch failed — {errors[0] if errors else '?'}"
            )
        status = ("sent" if deliver else "test send") + f" to {sent}"
        if failed:
            status += f" ({failed} failed)"
        logger.info(f"Resend: {status}")
        return MailResult(
            mailer=self.name,
            external_id=ids[0] if ids else "none",
            status=status,
            recipients=sent,
            failed=failed,
        )

    async def test_connection(self) -> bool:
        ok = True
        try:
            n = await asyncio.to_thread(self.subscribers.count)
            logger.info(f"subscribers: {n} confirmed in Supabase")
        except Exception as e:  # noqa: BLE001
            logger.error(f"subscribers table unreachable: {e}")
            ok = False
        # A sending-only key cannot read /domains; that is fine for this adapter.
        try:
            url = f"{self.api_base}/domains"
            if self._client is not None:
                resp = await self._client.get(url, headers=self._headers)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, headers=self._headers)
        except httpx.HTTPError as e:
            logger.error(f"Resend unreachable: {e}")
            return False
        domain = sender_domain(self.sender)
        if resp.status_code == 200:
            rows = (resp.json() or {}).get("data") or []
            verified = {
                str(d.get("name", "")).lower()
                for d in rows
                if isinstance(d, dict) and d.get("status") == "verified"
            }
            if domain not in verified:
                logger.error(
                    f"Resend: sender domain '{domain}' is not verified "
                    f"(verified: {sorted(verified) or 'none'})"
                )
                return False
            logger.info(f"Resend: sender domain {domain} verified")
        elif resp.status_code == 401 and "restricted_api_key" in resp.text:
            logger.info(
                f"Resend: sending-only key (cannot list domains); sender {self.sender} — "
                f"verify {domain} is 'verified' in the Resend dashboard"
            )
        else:
            logger.error(f"Resend returned {resp.status_code}: {self._snippet(resp)}")
            return False
        if not self.test_recipients:
            logger.warning(
                "NEWSLETTER_TEST_RECIPIENTS is empty: a draft delivery will have nowhere to go"
            )
        return ok
