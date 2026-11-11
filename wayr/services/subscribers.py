"""The subscriber list: who the newsletter is emailed to.

The site (its own repo) owns signups: a `subscribers` table in Supabase with
pending → confirmed → unsubscribed, double opt-in by email, and a per-row
`unsubscribe_token` that goes into every issue's unsubscribe link. Row-level
security hides the table from the site's publishable key; this module is the
one reader, with the service-role key, and the one query the site's contract
names: `select email, unsubscribe_token from subscribers where status = 'confirmed'`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from wayr.utils import get_logger

logger = get_logger(__name__)

PAGE = 1000  # PostgREST's default max rows per request


@dataclass(frozen=True)
class Subscriber:
    email: str
    token: str  # unsubscribe_token (a UUID the site minted at signup)


class SubscriberList:
    def __init__(
        self,
        url: str,
        secret_key: str,
        table: str = "subscribers",
        client=None,
    ):
        if not url or not secret_key:
            raise ValueError(
                "SubscriberList needs SUPABASE_URL and SUPABASE_SECRET_KEY"
            )
        self.table = table
        if client is None:
            from supabase import create_client

            client = create_client(url, secret_key)
        self.client = client

    def confirmed(self) -> List[Subscriber]:
        """Every confirmed subscriber, oldest first. Synchronous (supabase-py is)."""
        out: List[Subscriber] = []
        start = 0
        while True:  # bounded-loop-ok (ends on the first page shorter than PAGE)
            resp = (
                self.client.table(self.table)
                .select("email,unsubscribe_token")
                .eq("status", "confirmed")
                .order("created_at")
                .range(start, start + PAGE - 1)
                .execute()
            )
            rows = resp.data or []
            for r in rows:
                email = str(r.get("email") or "").strip().lower()
                token = str(r.get("unsubscribe_token") or "").strip()
                if email and token:
                    out.append(Subscriber(email=email, token=token))
                else:
                    # never log the row: it is an email address
                    logger.warning(
                        "subscribers: a confirmed row lacks email or token; skipped"
                    )
            if len(rows) < PAGE:
                break
            start += PAGE
        return out

    def count(self, status: Optional[str] = "confirmed") -> int:
        q = self.client.table(self.table).select("email", count="exact")
        if status:
            q = q.eq("status", status)
        resp = q.limit(1).execute()
        return int(resp.count or 0)
