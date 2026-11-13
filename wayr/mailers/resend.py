"""Resend mailer: one Broadcast per issue, to a Segment of contacts.

Facts from the Resend API docs (resend.com/docs, read 2026-11-10) that shape
this adapter:

- `POST /broadcasts` takes `segment_id`, `from` ("Name <addr>"), `subject`,
  `html` (and optional `text`, `reply_to`, `name`). With `send: true` the
  broadcast is sent immediately; without it, it is a draft in the dashboard.
  The response is `{"object": "broadcast", "id": ...}`.
- `GET /broadcasts/{id}` returns `status`: draft | scheduled | queued | sent |
  canceled. We read it once after creating, so the run log records what
  actually happened rather than what we asked for.
- Audiences became Segments; contacts are global and belong to segments.
  Unsubscribed contacts are skipped when a broadcast goes to a segment.
- `{{{RESEND_UNSUBSCRIBE_URL}}}` in the HTML becomes each recipient's own
  unsubscribe link. The renderer puts it in the footer; sending a broadcast
  without it is a deliverability (and legal) mistake, so this adapter refuses.
- Rate limit: 10 requests/second per team; we make two or three per issue.
- The connection test lists domains (the sender's domain must be `verified`)
  and fetches the segment, so a typo fails `wayr test`, not the first issue.
"""

import re
from typing import Any, Dict, Optional

import httpx

from wayr.mailers.base import MailError, MailResult, NewsletterMailer
from wayr.models.newsletter import NewsletterIssue
from wayr.utils import get_logger

logger = get_logger(__name__)

DEFAULT_API_BASE = "https://api.resend.com"
UNSUBSCRIBE_PLACEHOLDER = "{{{RESEND_UNSUBSCRIBE_URL}}}"

_ADDR = re.compile(
    r"<\s*([^<>@\s]+@([^<>@\s]+))\s*>\s*$|^\s*([^<>@\s]+@([^<>@\s]+))\s*$"
)


def sender_domain(sender: str) -> Optional[str]:
    """`WAYR <daily@wayrtoday.com>` → `wayrtoday.com`; None if it is not an address."""
    m = _ADDR.search(sender or "")
    if not m:
        return None
    return (m.group(2) or m.group(4) or "").lower() or None


class ResendMailer(NewsletterMailer):
    name = "resend"
    unsubscribe_placeholder = UNSUBSCRIBE_PLACEHOLDER

    def __init__(
        self,
        api_key: str,
        segment_id: str,
        sender: str,
        reply_to: Optional[str] = None,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        if not api_key or not segment_id or not sender:
            raise ValueError("ResendMailer needs api_key, segment_id and sender")
        if sender_domain(sender) is None:
            raise ValueError(
                f"NEWSLETTER_FROM must be an address or 'Name <address>', not {sender!r}"
            )
        self.api_key = api_key
        self.segment_id = segment_id
        self.sender = sender.strip()
        self.reply_to = (reply_to or "").strip() or None
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._client = client  # injectable for tests (httpx.MockTransport)

    @classmethod
    def from_settings(cls, settings) -> "ResendMailer":
        return cls(
            api_key=settings.resend_api_key or "",
            segment_id=settings.resend_segment_id or "",
            sender=settings.newsletter_from or "",
            reply_to=getattr(settings, "newsletter_reply_to", None),
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

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = f"{self.api_base}{path}"
        if self._client is not None:
            return await self._client.request(method, url, headers=self._headers, **kw)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.request(method, url, headers=self._headers, **kw)

    @staticmethod
    def _snippet(resp: httpx.Response) -> str:
        return resp.text[:300].replace("\n", " ")

    @staticmethod
    def _json(resp: httpx.Response) -> Dict[str, Any]:
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    # ---- contract ----

    async def send(
        self, issue: NewsletterIssue, html: str, *, deliver: bool = False
    ) -> MailResult:
        if UNSUBSCRIBE_PLACEHOLDER not in html:
            raise MailError(
                "email HTML has no unsubscribe link; render it with the email template"
            )
        subject = issue.email_subject
        payload: Dict[str, Any] = {
            "segment_id": self.segment_id,
            "from": self.sender,
            "subject": subject,
            "html": html,
            "name": f"{issue.title} — {issue.date_label}"[:120],
        }
        if self.reply_to:
            payload["reply_to"] = self.reply_to
        if deliver:
            payload["send"] = True
        logger.info(
            f"Resend: creating broadcast '{subject[:70]}' ({len(html)} bytes) "
            f"to segment {self.segment_id}"
            + (", sending now" if deliver else " as a draft")
        )

        try:
            resp = await self._request("POST", "/broadcasts", json=payload)
        except httpx.HTTPError as e:
            raise MailError(f"Resend create failed: {e}") from e
        if resp.status_code not in (200, 201):
            raise MailError(
                f"Resend create returned {resp.status_code}: {self._snippet(resp)}"
            )
        broadcast_id = str(self._json(resp).get("id") or "").strip()
        if not broadcast_id:
            raise MailError(f"Resend returned no broadcast id: {self._snippet(resp)}")

        status = await self._status(
            broadcast_id, default="queued" if deliver else "draft"
        )
        if deliver and status in ("draft", "canceled"):
            # `send: true` was accepted but the broadcast did not go out — surface it.
            raise MailError(
                f"Resend broadcast {broadcast_id} is '{status}' after a send request"
            )
        logger.info(f"Resend: broadcast {broadcast_id} is '{status}'")
        return MailResult(
            mailer=self.name,
            external_id=broadcast_id,
            status=status,
            url=f"https://resend.com/broadcasts/{broadcast_id}",
        )

    async def _status(self, broadcast_id: str, default: str) -> str:
        """One read of the broadcast; the default stands if the read fails."""
        try:
            resp = await self._request("GET", f"/broadcasts/{broadcast_id}")
        except httpx.HTTPError as e:
            logger.warning(f"Resend: could not read broadcast {broadcast_id}: {e}")
            return default
        if resp.status_code != 200:
            logger.warning(
                f"Resend: broadcast read returned {resp.status_code}: {self._snippet(resp)}"
            )
            return default
        return str(self._json(resp).get("status") or default)

    async def test_connection(self) -> bool:
        domain = sender_domain(self.sender)
        try:
            domains = await self._request("GET", "/domains")
            segment = await self._request("GET", f"/segments/{self.segment_id}")
        except httpx.HTTPError as e:
            logger.error(f"Resend connection failed: {e}")
            return False
        if domains.status_code != 200:
            hint = ""
            if "restricted_api_key" in domains.text:
                hint = (
                    " — this key has 'Sending access' only, which covers POST /emails; "
                    "broadcasts, segments and domains need a 'Full access' key"
                )
            logger.error(
                f"Resend domains returned {domains.status_code}: {self._snippet(domains)}{hint}"
            )
            return False
        if segment.status_code != 200:
            logger.error(
                f"Resend segment {self.segment_id} returned {segment.status_code}: "
                f"{self._snippet(segment)}"
            )
            return False
        rows = self._json(domains).get("data") or []
        verified = {
            str(d.get("name", "")).lower()
            for d in rows
            if isinstance(d, dict) and d.get("status") == "verified"
        }
        if domain not in verified:
            logger.error(
                f"Resend: sender domain '{domain}' is not verified "
                f"(verified: {sorted(verified) or 'none'}); add its DNS records in Resend"
            )
            return False
        logger.info(
            f"Resend: segment '{self._json(segment).get('name', self.segment_id)}', "
            f"sender domain {domain} verified"
        )
        return True
