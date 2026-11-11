"""URL-based hashing used as the stable article identifier across the pipeline.

Both the local ArticleTracker cache and Supabase pipeline rows key by this
same hash, so anything joining the two sides must use this helper.
"""

import hashlib


def url_hash(url: str) -> str:
    """Return the canonical 16-char hash for a URL.

    Lowercases and strips whitespace so trivial URL variants collapse to the
    same key. Used as the primary identifier in both the on-disk cache and
    Supabase; changing this function silently invalidates every existing row.
    """
    normalized = url.lower().strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]
