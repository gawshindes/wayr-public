"""Small text helpers shared across services and agents."""

import html
import re
from typing import Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


def html_to_text(html_text: Optional[str]) -> str:
    """Feed bodies are HTML: strip tags, unescape, keep paragraph breaks."""
    if not html_text:
        return ""
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|blockquote|pre|tr)>", "\n\n", html_text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    paragraphs = [re.sub(r"[ \t]+", " ", p).strip() for p in text.split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


def read_minutes(text: str, wpm: int = 230) -> int:
    """Reading time of a piece of text, never below one minute."""
    words = len((text or "").split())
    return max(1, round(words / wpm))


def clean_summary(text: Optional[str], max_chars: int = 500) -> str:
    """Strip tags and entities, collapse whitespace, cap length.

    Feed descriptions are often HTML fragments with entities. Used wherever a
    feed summary is persisted or shown to an LLM.
    """
    if not text:
        return ""
    cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(text))).strip()
    cleaned = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", cleaned)
    return cleaned[:max_chars]
