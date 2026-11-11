"""Beehiiv draft publisher.

Facts from the v2 API docs that shape this adapter:

- Create Post takes `title` plus `body_content` (raw HTML). It has no status,
  subject_line, subtitle or content_tags parameters, so a created post cannot
  be confirmed or sent through this call — it lands as a draft, which is
  exactly what we want. The subject line therefore goes in `title` (Beehiiv
  defaults the email subject to it) and the intro is already the body's first
  paragraph.
- Creation is asynchronous: 201 with the post `id`, then GET returns 202 while
  processing, 200 when done, or 404 with code POST_CREATION_FAILED. We poll
  briefly to confirm the draft exists and to pick up `web_url`; a creation
  failure is a failed publish, not a false success.
- `<style>` blocks and `<link>` tags are stripped on ingest; only inline
  `style=` attributes survive.
- **Plan gate (found in E2E):** Create Post is part of Beehiiv's "Send API",
  available only on the Max and Enterprise plans. Launch and Scale get a
  read-only API and return 403 `SEND_API_NOT_ENTERPRISE_PLAN`. "RSS to Send" is
  gated the same way, so there is no automated path into a lower-plan
  publication. `test_connection()` cannot detect this: it hits a read endpoint.
"""

import asyncio
from typing import Any, Dict, Optional

import httpx

from wayr.models.newsletter import NewsletterIssue
from wayr.publishers.base import NewsletterPublisher, PublishError, PublishResult
from wayr.utils import get_logger

logger = get_logger(__name__)

DEFAULT_API_BASE = "https://api.beehiiv.com/v2"


class BeehiivPublisher(NewsletterPublisher):
    name = "beehiiv"

    def __init__(
        self,
        api_key: str,
        publication_id: str,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 30.0,
        poll_attempts: int = 5,
        poll_interval: float = 2.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        if not api_key or not publication_id:
            raise ValueError("BeehiivPublisher needs api_key and publication_id")
        self.api_key = api_key
        self.publication_id = publication_id
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.poll_attempts = poll_attempts
        self.poll_interval = poll_interval
        self._client = client  # injectable for tests (httpx.MockTransport)

    @classmethod
    def from_settings(cls, settings) -> "BeehiivPublisher":
        if getattr(settings, "newsletter_delivery", "draft") == "publish":
            logger.warning(
                "NEWSLETTER_DELIVERY=publish: the Beehiiv adapter creates drafts only "
                "until sending is verified against a paid Max/Enterprise plan"
            )
        return cls(
            api_key=settings.beehiiv_api_key or "",
            publication_id=settings.beehiiv_publication_id or "",
            api_base=getattr(settings, "beehiiv_api_base", DEFAULT_API_BASE),
        )

    # ---- http ----

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _posts_url(self, post_id: Optional[str] = None) -> str:
        base = f"{self.api_base}/publications/{self.publication_id}/posts"
        return f"{base}/{post_id}" if post_id else base

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, url, headers=self._headers, **kw)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await client.request(method, url, headers=self._headers, **kw)

    @staticmethod
    def _snippet(resp: httpx.Response) -> str:
        return resp.text[:300].replace("\n", " ")

    # ---- contract ----

    async def publish(self, issue: NewsletterIssue, html: str) -> PublishResult:
        title = (issue.subject_line or issue.title).strip() or issue.title
        payload = {"title": title, "body_content": html}
        logger.info(f"Beehiiv: creating draft '{title[:70]}' ({len(html)} bytes)")

        try:
            resp = await self._request("POST", self._posts_url(), json=payload)
        except httpx.HTTPError as e:
            raise PublishError(f"Beehiiv create failed: {e}") from e
        if resp.status_code not in (200, 201):
            raise PublishError(
                f"Beehiiv create returned {resp.status_code}: {self._snippet(resp)}"
            )

        body = resp.json() if resp.content else {}
        data = body.get("data", body) if isinstance(body, dict) else {}
        post_id = str(data.get("id") or "").strip()
        if not post_id:
            raise PublishError(
                f"Beehiiv create returned no post id: {self._snippet(resp)}"
            )
        logger.info(f"Beehiiv: accepted, post id {post_id}; confirming creation")

        status, url = await self._confirm(post_id)
        return PublishResult(
            publisher=self.name, external_id=post_id, url=url, status=status
        )

    async def _confirm(self, post_id: str) -> tuple[str, Optional[str]]:
        """Poll until the async creation settles. Returns (status, web_url)."""
        for attempt in range(1, self.poll_attempts + 1):
            try:
                resp = await self._request("GET", self._posts_url(post_id))
            except httpx.HTTPError as e:
                logger.warning(f"Beehiiv: poll {attempt} errored: {e}")
                resp = None

            if resp is not None:
                if resp.status_code == 200:
                    body = resp.json() if resp.content else {}
                    data = body.get("data", body) if isinstance(body, dict) else {}
                    status = str(data.get("status") or "draft")
                    url = data.get("web_url") or None
                    logger.info(
                        f"Beehiiv: post {post_id} is '{status}'"
                        + (f" at {url}" if url else "")
                    )
                    return status, url
                if resp.status_code == 404:
                    code = ""
                    try:
                        code = str((resp.json() or {}).get("code") or "")
                    except ValueError:
                        pass
                    if "POST_CREATION_FAILED" in code or "CREATION_FAILED" in resp.text:
                        raise PublishError(
                            f"Beehiiv reported creation failed for post {post_id}: {self._snippet(resp)}"
                        )
                    # plain 404 right after 201: not visible yet — keep polling
                elif resp.status_code != 202:
                    raise PublishError(
                        f"Beehiiv poll returned {resp.status_code}: {self._snippet(resp)}"
                    )

            if attempt < self.poll_attempts:
                await asyncio.sleep(self.poll_interval)

        logger.warning(
            f"Beehiiv: post {post_id} still processing after {self.poll_attempts} polls; "
            f"treating as accepted (check the Beehiiv dashboard)"
        )
        return "accepted", None

    async def test_connection(self) -> bool:
        try:
            resp = await self._request(
                "GET", f"{self.api_base}/publications/{self.publication_id}"
            )
        except httpx.HTTPError as e:
            logger.error(f"Beehiiv connection failed: {e}")
            return False
        if resp.status_code == 200:
            return True
        logger.error(
            f"Beehiiv connection returned {resp.status_code}: {self._snippet(resp)}"
        )
        return False
