"""What a published issue says about itself on social media.

The digest post is one link a day; LinkedIn is where the site's readers come
from. The copy is the editor's own opening (already in the issue's voice) and
the headlines, nothing generated here — no model call, no new voice.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional
from urllib.parse import urlparse

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


def public_url(
    template: Optional[str], slug: Optional[str], fallback: Optional[str]
) -> Optional[str]:
    """The reader-facing URL of a published issue: the template with the post's slug
    when both exist (the site is not the CMS), else the publisher's own URL."""
    if template and "{slug}" in template:
        if not slug and fallback:
            slug = urlparse(fallback).path.rstrip("/").rsplit("/", 1)[-1] or None
        if slug:
            return template.replace("{slug}", slug)
    return fallback


def social_copy(issue: NewsletterIssue, label: str = "") -> SocialCopy:
    title = _plain(issue.subject_line or issue.title) or issue.title
    intro = _plain(issue.intro or "")
    heads = headlines(issue)
    label = (label or "").strip()

    parts: List[str] = []
    if label:
        # Readers see a single-story opening otherwise; say what this is first.
        parts.append(f"{label} · {issue.date_label}" if issue.date_label else label)
    if intro:
        parts.append(intro)
    if heads:
        parts.append("In today's issue:\n" + "\n".join(f"• {h}" for h in heads))
    linkedin = "\n\n".join(parts) or title
    if len(linkedin) > LINKEDIN_MAX:
        linkedin = linkedin[:LINKEDIN_MAX].rsplit("\n", 1)[0]

    tweet = heads[0] if heads else (intro or title)
    if label:
        tweet = f"{label}: {tweet}"
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


async def _status(url: str) -> int:
    import httpx

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        return (
            await client.get(url, headers={"User-Agent": "WAYR link check"})
        ).status_code


async def wait_until_live(
    url: str,
    timeout: float,
    interval: float = 15.0,
    fetch: Optional[Callable[[str], Awaitable[int]]] = None,
) -> bool:
    """Poll `url` until it answers 200 or `timeout` seconds pass. A site with a
    cache in front of the CMS can take minutes to show a new post, and LinkedIn
    builds its link card at post time from whatever the URL returns then."""
    fetch = fetch or _status
    deadline = asyncio.get_event_loop().time() + max(0.0, timeout)
    while True:  # bounded-loop-ok (returns at 200 or when the deadline passes)
        try:
            code = await fetch(url)
        except Exception:  # noqa: BLE001 - a network blip is "not yet"
            code = 0
        if code == 200:
            return True
        if asyncio.get_event_loop().time() + interval > deadline:
            return False
        await asyncio.sleep(interval)
