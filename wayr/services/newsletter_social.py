"""What a published issue says about itself on social media.

The digest post is one link a day; LinkedIn is where the site's readers come
from. The copy is the editor's own opening (already in the issue's voice) and
the headlines, nothing generated here — no model call, no new voice.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import List

from wayr.models.newsletter import NewsletterIssue

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_TAGS = re.compile(r"<[^>]+>")
LINKEDIN_MAX = 2900  # the API allows 3000; leave room
MAX_HEADLINES = 6


def _plain(text: str) -> str:
    text = _TAGS.sub("", text or "")
    text = _BOLD.sub(r"\1", text)
    return html.unescape(text).strip()


@dataclass
class SocialCopy:
    title: str  # the link card's title
    linkedin: str  # commentary above the card
    tweet: str  # ≤150 chars; the tweet formatter adds the URL
    description: str  # the link card's description (≤200 chars)


def headlines(issue: NewsletterIssue, limit: int = MAX_HEADLINES) -> List[str]:
    """Written headlines of the feature stories, in issue order."""
    out: List[str] = []
    for section in issue.sections:
        if section.is_placeholder or section.style != "feature":
            continue
        for story in section.stories:
            out.append(_plain(story.display_headline))
            if len(out) >= limit:
                return out
    return out


def social_copy(issue: NewsletterIssue) -> SocialCopy:
    title = _plain(issue.subject_line or issue.title) or issue.title
    intro = _plain(issue.intro or "")
    heads = headlines(issue)

    parts: List[str] = []
    if intro:
        parts.append(intro)
    if heads:
        parts.append("In today's issue:\n" + "\n".join(f"• {h}" for h in heads))
    linkedin = "\n\n".join(parts) or title
    if len(linkedin) > LINKEDIN_MAX:
        linkedin = linkedin[:LINKEDIN_MAX].rsplit("\n", 1)[0]

    tweet = heads[0] if heads else (intro or title)
    if len(tweet) > 150:
        tweet = tweet[:150].rsplit(" ", 1)[0] + "…"

    first_blurb = ""
    for section in issue.sections:
        for story in section.stories:
            if story.blurb:
                first_blurb = _plain(story.blurb)
                break
        if first_blurb:
            break
    description = (intro or first_blurb or title)[:200]
    return SocialCopy(
        title=title, linkedin=linkedin, tweet=tweet, description=description
    )
