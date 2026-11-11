"""Mailer adapter contract: how a published issue reaches subscribers' inboxes.

A publisher (wayr.publishers) puts the issue on the web — a WordPress post, a
Beehiiv post. A mailer sends that same issue to a list. The two are separate
seams because a site that already publishes on WordPress only lacks the
sending: the issue lives at a URL, the email is the same HTML wrapped with a
"read on the web" link and an unsubscribe footer, and the list belongs to the
email provider. NEWSLETTER_MAILER picks the adapter (none | resend).
"""

from abc import ABC, abstractmethod
from typing import ClassVar, Optional

from pydantic import BaseModel

from wayr.models.newsletter import NewsletterIssue


class MailError(RuntimeError):
    """A send attempt failed. The message is safe to log and store."""


class MailResult(BaseModel):
    mailer: str  # registry name, e.g. "resend"
    external_id: str  # the broadcast/campaign id on the provider
    status: str = (
        "draft"  # the provider's own status word (draft | queued | sent | scheduled)
    )
    url: Optional[str] = None  # a dashboard or archive URL if the provider gives one
    recipients: Optional[int] = (
        None  # emails sent, when the mailer addresses them itself
    )
    failed: int = 0


class NewsletterMailer(ABC):
    """Every mailer implements exactly this; the orchestrator knows nothing else."""

    name: ClassVar[str]
    # The literal the provider substitutes per recipient with their unsubscribe
    # link; the renderer places it in the email footer.
    unsubscribe_placeholder: ClassVar[str] = "#"

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> "NewsletterMailer":
        """Build from env-loaded settings. Raise if credentials are missing."""

    @abstractmethod
    async def send(
        self, issue: NewsletterIssue, html: str, *, deliver: bool = False
    ) -> MailResult:
        """Create the email for the list. `deliver=True` sends it now; otherwise it
        is left as a draft on the provider for a human to send. Raise MailError."""

    @abstractmethod
    async def test_connection(self) -> bool:
        """True if the credentials work and the list and sender exist."""
