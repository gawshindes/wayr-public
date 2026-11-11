"""Publisher adapter contract for newsletter issues."""

from abc import ABC, abstractmethod
from typing import ClassVar, Optional

from pydantic import BaseModel

from wayr.models.newsletter import NewsletterIssue


class PublishError(RuntimeError):
    """A publish attempt failed. The message is safe to log and store."""


class PublishResult(BaseModel):
    publisher: str  # registry name, e.g. "beehiiv"
    external_id: str  # the post id on the target platform
    url: Optional[str] = None  # public/web URL if the platform returned one
    status: str = "draft"  # the platform's own status word, or "accepted"


class NewsletterPublisher(ABC):
    """Every publisher implements exactly this; the orchestrator knows nothing else."""

    name: ClassVar[str]

    @classmethod
    @abstractmethod
    def from_settings(cls, settings) -> "NewsletterPublisher":
        """Build from env-loaded settings. Raise if credentials are missing."""

    @abstractmethod
    async def publish(self, issue: NewsletterIssue, html: str) -> PublishResult:
        """Push the issue as a draft. Raise PublishError on failure."""

    @abstractmethod
    async def test_connection(self) -> bool:
        """True if credentials work against the live API."""
