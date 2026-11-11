"""Discover and validate RSS feeds for a newsletter's source list.

Two inputs, either or both:
  --from-describe JSON  a describe_newsletter.py --json output; domains are ranked by
                        how often the named newsletters linked to them
  --seed DOMAIN         extra domains to try (a known local outlet, a must-have source)

For every domain: find feed URLs (the page's <link rel="alternate">, then well-known
paths), validate each with feedparser (entries, newest item age, average words in the
feed's own text), and write a candidates file in RSS_FEEDS_FILE format — one URL per
line with a comment giving the evidence — plus the rejects as comments at the end.
A human reviews it and renames it sources.txt. Never trusts a feed it did not parse.

    poetry run python scripts/find_feeds.py --from-describe .cache/corpus_run6.json \
        --newsletter neuron --out newsletters/ai-daily/sources.candidates.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Set
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from wayr.models.sources import Source  # noqa: E402

UA = "Mozilla/5.0 (compatible; wayr-find-feeds/1.0)"
WELL_KNOWN = (
    "/feed/",
    "/feed",
    "/rss/",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/feeds/posts/default",
    "/?feed=rss2",
    "/blog/feed/",
    "/news/feed/",
)
# Never a news source: platforms, socials, the newsletters' own hosts, link shorteners.
SKIP = {
    "beehiiv.com",
    "substack.com",
    "mailchimp.com",
    "list-manage.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "threads.net",
    "reddit.com",
    "tiktok.com",
    "bit.ly",
    "t.co",
    "skimmth.is",
    "smartbrief.com",
    "tldr.tech",
    "tldrnewsletter.com",
    "theneurondaily.com",
    "theneuron.ai",
    "therundown.ai",
}


@dataclass
class FeedCheck:
    url: str
    ok: bool
    entries: int = 0
    newest_hours: Optional[float] = None
    avg_words: int = 0
    title: str = ""
    error: str = ""


@dataclass
class DomainResult:
    domain: str
    linked: int
    feeds: List[FeedCheck] = field(default_factory=list)
    note: str = ""

    @property
    def best(self) -> Optional[FeedCheck]:
        good = [f for f in self.feeds if f.ok]
        return (
            max(good, key=lambda f: (f.entries, -(f.newest_hours or 9e9)))
            if good
            else None
        )


def domains_from_describe(path: str, newsletters: Set[str]) -> Counter:
    issues = json.load(open(path, encoding="utf-8"))
    counts: Counter = Counter()
    for issue in issues:
        if newsletters and issue.get("newsletter") not in newsletters:
            continue
        for block in issue.get("blocks", []):
            for link in block.get("links", []):
                d = (link.get("domain") or "").lower()
                if d:
                    counts[d] += 1
    return counts


def root_domain(d: str) -> str:
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


# Platforms whose root is a store, not a newsroom — skipped unless collecting brands.
PLATFORMS = {"google.com", "apple.com", "amazon.com"}


def skip(domain: str, origin: str = "press") -> bool:
    blocked = SKIP if origin == "brand" else SKIP | PLATFORMS
    return any(domain == s or domain.endswith("." + s) for s in blocked)


def discover(domain: str, timeout: int = 15) -> List[str]:
    """Candidate feed URLs for a domain: declared <link rel=alternate>, then well-known paths."""
    found: List[str] = []
    base = f"https://{domain}/"
    try:
        r = requests.get(
            base, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True
        )
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup.find_all(
                "link", attrs={"rel": lambda v: v and "alternate" in v}
            ):
                typ = (tag.get("type") or "").lower()
                href = tag.get("href")
                if href and ("rss" in typ or "atom" in typ or "xml" in typ):
                    found.append(urljoin(r.url, href))
            base = r.url  # follow the canonical host
    except requests.RequestException:
        pass
    for p in WELL_KNOWN:
        u = urljoin(base, p)
        if u not in found:
            found.append(u)
    return found


def check(url: str, timeout: int = 20) -> FeedCheck:
    try:
        r = requests.get(
            url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True
        )
    except requests.RequestException as e:
        return FeedCheck(url, False, error=f"request: {e.__class__.__name__}")
    if r.status_code != 200:
        return FeedCheck(url, False, error=f"HTTP {r.status_code}")
    ctype = (r.headers.get("content-type") or "").lower()
    if (
        "html" in ctype
        and "xml" not in ctype
        and not r.text.lstrip().startswith("<?xml")
    ):
        return FeedCheck(url, False, error="HTML, not a feed")
    fp = feedparser.parse(r.content)
    entries = fp.entries or []
    if not entries:
        return FeedCheck(
            url, False, error="parsed but no entries" + (" (bozo)" if fp.bozo else "")
        )
    now = datetime.now(timezone.utc)
    ages = []
    words = []
    for e in entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            ages.append(
                (now - datetime(*t[:6], tzinfo=timezone.utc)).total_seconds() / 3600
            )
        text = ""
        if e.get("content"):
            text = " ".join(c.get("value", "") for c in e["content"])
        text = text or e.get("summary", "") or ""
        words.append(len(re.sub(r"<[^>]+>", " ", text).split()))
    return FeedCheck(
        url=r.url,
        ok=True,
        entries=len(entries),
        newest_hours=round(min(ages), 1) if ages else None,
        avg_words=int(sum(words) / len(words)) if words else 0,
        title=(fp.feed.get("title") or "")[:60],
    )


def probe(
    domain: str, linked: int, max_candidates: int, origin: str = "press"
) -> DomainResult:
    res = DomainResult(domain=domain, linked=linked)
    if skip(domain, origin):
        res.note = "skipped (platform/social/self)"
        return res
    cands = discover(domain)[:max_candidates]
    seen: Set[str] = set()
    for u in cands:
        if u in seen:
            continue
        seen.add(u)
        fc = check(u)
        res.feeds.append(fc)
        if fc.ok and fc.entries >= 5:
            break  # first good feed wins; well-known paths are ordered by likelihood
        time.sleep(0.3)
    return res


def fmt_age(h: Optional[float]) -> str:
    if h is None:
        return "no dates"
    return f"{h:.0f}h" if h < 72 else f"{h / 24:.0f}d"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--from-describe", help="describe_newsletter.py --json output")
    ap.add_argument(
        "--newsletter",
        action="append",
        default=[],
        help="newsletter name(s) in that JSON to draw links from",
    )
    ap.add_argument(
        "--seed", action="append", default=[], help="extra domain to try (repeatable)"
    )
    ap.add_argument(
        "--feed",
        action="append",
        default=[],
        help="exact feed URL to validate (repeatable)",
    )
    ap.add_argument(
        "--origin",
        choices=("press", "brand"),
        default="press",
        help="what these sources are: press (news sites, default) or brand (companies' own newsrooms); emitted as origin= on every line",
    )
    ap.add_argument(
        "--out", required=True, help="candidates file to write (RSS_FEEDS_FILE format)"
    )
    ap.add_argument(
        "--min-linked",
        type=int,
        default=1,
        help="only domains linked at least this often",
    )
    ap.add_argument("--max-domains", type=int, default=40)
    ap.add_argument("--min-items", type=int, default=5)
    ap.add_argument("--max-age-days", type=float, default=14)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument(
        "--max-candidates", type=int, default=8, help="feed URLs to try per domain"
    )
    args = ap.parse_args()

    counts: Counter = Counter()
    if args.from_describe:
        raw = domains_from_describe(args.from_describe, set(args.newsletter))
        for d, n in raw.items():
            counts[root_domain(d)] += n
    for s in args.seed:
        counts[root_domain(s.lower())] += 0
    domains = [
        (d, n)
        for d, n in counts.most_common()
        if n >= args.min_linked or d in {root_domain(s) for s in args.seed}
    ]
    domains = domains[: args.max_domains]
    print(
        f"probing {len(domains)} domains ({args.workers} workers)...", file=sys.stderr
    )

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(
            ex.map(
                lambda dn: probe(dn[0], dn[1], args.max_candidates, args.origin),
                domains,
            )
        )
        for u, fc in zip(args.feed, ex.map(check, args.feed)):
            r = DomainResult(domain=urlparse(u).netloc.removeprefix("www."), linked=0)
            r.feeds.append(fc)
            results.append(r)

    keep, weak, reject = [], [], []
    for r in results:
        b = r.best
        if r.note:
            reject.append((r, r.note))
        elif b is None:
            errs = "; ".join(sorted({f.error for f in r.feeds}))[:80]
            reject.append((r, f"no feed ({errs})"))
        elif b.entries < args.min_items or (
            b.newest_hours is not None and b.newest_hours > args.max_age_days * 24
        ):
            weak.append((r, b))
        else:
            keep.append((r, b))

    lines = [
        "# Feed candidates — review, prune, rename to sources.txt (RSS_FEEDS_FILE format).",
        f"# Generated {datetime.now(timezone.utc):%Y-%m-%d} from: "
        + (
            f"{', '.join(args.newsletter) or 'all'} in {args.from_describe}"
            if args.from_describe
            else "seeds only"
        )
        + (f"; seeds: {', '.join(args.seed)}" if args.seed else ""),
        "# Per line: feed URL [origin=brand]  # domain · items · newest · avg words in feed text · times linked in the corpus",
        "# The pipeline extracts full article text itself; feed word counts only show how much the feed carries.",
        "",
    ]
    for r, b in sorted(keep, key=lambda t: -t[0].linked):
        lines.append(
            f"{Source(url=b.url, origin=args.origin).to_line()}  # {r.domain} · {b.entries} items · newest {fmt_age(b.newest_hours)} · ~{b.avg_words}w · linked {r.linked}×"
        )
    if weak:
        lines += ["", "# Weak — few items or stale; keep only if you know the outlet:"]
        for r, b in sorted(weak, key=lambda t: -t[0].linked):
            lines.append(
                f"# {Source(url=b.url, origin=args.origin).to_line()}  # {r.domain} · {b.entries} items · newest {fmt_age(b.newest_hours)} · linked {r.linked}×"
            )
    if reject:
        lines += ["", "# No usable feed found:"]
        for r, why in sorted(reject, key=lambda t: -t[0].linked):
            lines.append(f"#   {r.domain} (linked {r.linked}×): {why}")

    from pathlib import Path

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"{len(keep)} feeds kept, {len(weak)} weak, {len(reject)} domains without a feed → {args.out}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
