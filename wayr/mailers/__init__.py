"""Newsletter mailers: how a published issue reaches subscribers.

One mailer per deployment, selected by NEWSLETTER_MAILER and resolved through a
name → class registry, like wayr.publishers:

  none            the issue lives only where the publisher put it (default)
  resend          one Resend Broadcast to a Segment — the list lives at Resend
  resend-direct   one email per confirmed subscriber in our own Supabase table,
                  through Resend's batch endpoint — the site owns the list
"""

from typing import Dict, Optional, Type

from wayr.mailers.base import MailError, MailResult, NewsletterMailer
from wayr.mailers.resend import ResendMailer
from wayr.mailers.resend_direct import ResendDirectMailer

REGISTRY: Dict[str, Type[NewsletterMailer]] = {
    ResendMailer.name: ResendMailer,
    ResendDirectMailer.name: ResendDirectMailer,
}

NONE = "none"


def get_newsletter_mailer(settings) -> Optional[NewsletterMailer]:
    """Build the configured mailer, or None when NEWSLETTER_MAILER=none.
    Unknown names fail at startup, not at send time."""
    name = (getattr(settings, "newsletter_mailer", NONE) or NONE).strip().lower()
    if name == NONE:
        return None
    if name not in REGISTRY:
        raise ValueError(
            f"Unknown NEWSLETTER_MAILER '{name}'. Available: {sorted(REGISTRY)} or {NONE}"
        )
    return REGISTRY[name].from_settings(settings)


__all__ = [
    "NewsletterMailer",
    "MailError",
    "MailResult",
    "ResendMailer",
    "ResendDirectMailer",
    "REGISTRY",
    "NONE",
    "get_newsletter_mailer",
]
