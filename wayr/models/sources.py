"""Typed sources: what the sources file says about each feed.

A line in ``RSS_FEEDS_FILE`` is a URL followed by optional ``key=value`` tokens
and an optional ``# comment``::

    https://techcrunch.com/feed/                           # plain = type=rss origin=press
    https://openai.com/news/rss.xml         origin=brand
    https://www.anthropic.com/news          type=page origin=brand

``type`` says how to fetch (``rss`` today; ``page`` is reserved for a
listing-page adapter and is parsed but skipped). ``origin`` says where the
words come from: ``press`` is a news site covering events, ``brand`` is a
company announcing its own. The origin travels with every article the source
yields, and three things read it: the news-type classifier (a brand page's
title *is* the announcement, its body is often marketing copy), the dedup
keeper (the brand URL wins a cluster, so the newsletter links the primary
source), and the evals.

The parser is loud on purpose. A typo (``origin:brand``, ``type=atom``) is a
configuration error, not something to guess at.
"""

from __future__ import annotations

from typing import Iterable, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

SOURCE_TYPES = ("rss", "page")
SOURCE_ORIGINS = ("press", "brand")

SourceType = Literal["rss", "page"]
SourceOrigin = Literal["press", "brand"]

_ALLOWED = {"type": SOURCE_TYPES, "origin": SOURCE_ORIGINS}


class Source(BaseModel):
    """One configured source: where it is, how to fetch it, whose words it carries."""

    model_config = ConfigDict(frozen=True)

    url: str
    type: SourceType = "rss"
    origin: SourceOrigin = "press"

    def to_line(self) -> str:
        """Canonical sources-file line: the URL plus any non-default tokens."""
        parts = [self.url]
        if self.type != "rss":
            parts.append(f"type={self.type}")
        if self.origin != "press":
            parts.append(f"origin={self.origin}")
        return "  ".join(parts)


def _where(filename: str, lineno: int) -> str:
    if filename and lineno:
        return f"{filename}:{lineno}: "
    if filename:
        return f"{filename}: "
    return f"line {lineno}: " if lineno else ""


def parse_source_line(
    line: str, *, lineno: int = 0, filename: str = ""
) -> Optional[Source]:
    """Parse one line. Returns None for blank/comment lines; raises ValueError
    on anything that is not a URL followed by known ``key=value`` tokens."""
    body = line.split("#", 1)[0].strip()
    if not body:
        return None
    tokens = body.split()
    url = tokens[0]
    where = _where(filename, lineno)
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"{where}not a URL: {url!r}")
    fields: dict = {}
    for tok in tokens[1:]:
        if "=" not in tok:
            raise ValueError(
                f"{where}expected key=value after the URL, got {tok!r} "
                f"(known keys: {', '.join(_ALLOWED)})"
            )
        key, value = tok.split("=", 1)
        if key not in _ALLOWED:
            raise ValueError(
                f"{where}unknown key {key!r} (known keys: {', '.join(_ALLOWED)})"
            )
        if value not in _ALLOWED[key]:
            raise ValueError(
                f"{where}{key}={value!r} is not one of {', '.join(_ALLOWED[key])}"
            )
        if key in fields:
            raise ValueError(f"{where}{key}= given twice")
        fields[key] = value
    return Source(url=url, **fields)


def parse_sources_text(text: str, *, filename: str = "") -> List[Source]:
    """Parse a whole sources file. Identical repeats collapse; the same URL
    with different tokens is an error, because one of them is wrong."""
    out: List[Source] = []
    by_url: dict = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        src = parse_source_line(line, lineno=lineno, filename=filename)
        if src is None:
            continue
        seen = by_url.get(src.url)
        if seen is None:
            by_url[src.url] = src
            out.append(src)
        elif seen != src:
            raise ValueError(
                f"{_where(filename, lineno)}{src.url} is defined twice with "
                f"different tokens ({seen.to_line()!r} vs {src.to_line()!r})"
            )
    return out


def sources_from_urls(urls: Iterable[str]) -> List[Source]:
    """Bare URLs (the RSS_FEEDS comma list) are rss/press sources."""
    out: List[Source] = []
    seen = set()
    for u in urls:
        u = u.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(Source(url=u))
    return out
