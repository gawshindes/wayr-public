"""Describe the structure of published newsletter issues.

Deliberately *descriptive*, not prescriptive. This tool does NOT map what it
finds onto the section schema in docs/newsletter-mode-plan.md — it reports raw
observations (headings in order, links with the text they are anchored on, item
counts, block composition) and lets the taxonomy fall out of the aggregate.

That distinction is the whole point. An extractor built to classify pages into
the styles we already believe in can only ever confirm them; a weekday-varying
placeholder slot or a fifth item style would be silently normalised away. Once
enough issues have been described, the mapping layer goes on top of this.

Usage:
    poetry run python scripts/describe_newsletter.py URL [URL ...]
    poetry run python scripts/describe_newsletter.py --csv corpus.csv
    poetry run python scripts/describe_newsletter.py --csv corpus.csv --json out.json

CSV format — `newsletter` and `issue_url` are required, everything else is
optional metadata carried through to the JSON and the reports:

    newsletter,issue_url,type,frequency,platform,notes
    neuron,https://www.theneurondaily.com/p/openclaw-2-0-is-here,ai-daily,daily,beehiiv,
    neuron,https://www.theneurondaily.com/p/...,ai-daily,daily,beehiiv,
    technically-austin,https://technically-austin.beehiiv.com/p/...,local-tech,weekly,beehiiv,

Three report levels are produced:
  1. per-issue    — every block, in order, with its links
  2. per-newsletter stability — which slots recur across issues of the SAME title
  3. corpus style signals — words-per-link and anchor behaviour across every
     section of every newsletter, which is what the item-style taxonomy should
     be derived from

Fetches are cached under .cache/newsletters/ so re-running the analysis costs
nothing and does not re-hammer publishers.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag

HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]

# A heading matching one of these marks the end of the issue proper — what
# follows is the hosting site's chrome (related posts, archive links, share
# prompts), not newsletter structure. Parsing stops there.
TRAILER_PATTERNS = re.compile(
    r"^(you might also like|related( posts| articles)?|more from|read more|"
    r"recent posts|previous issues?|share this|discussion about this post|"
    r"ready for more\??)\b",
    re.IGNORECASE,
)
UA = "Mozilla/5.0 (compatible; wayr-newsletter-describe/1.0)"
CACHE_DIR = Path(".cache/newsletters")


@dataclass
class Link:
    text: str
    href: str
    domain: str
    # Raw signals for deriving the anchor taxonomy later — not a classification.
    anchor_matches_domain: bool
    position_in_block: Optional[float]  # 0.0 = block start, 1.0 = block end
    on_heading: bool = False  # the heading itself is the link (<a><h3> or <h3><a>)


@dataclass
class Block:
    heading: str
    heading_level: Optional[str]
    word_count: int
    links: List[Link] = field(default_factory=list)
    images: int = 0
    buttons: int = 0
    code_blocks: int = 0
    list_items: int = 0

    def external_links(self) -> List[Link]:
        return [link for link in self.links if link.domain]

    def words_per_link(self) -> Optional[float]:
        ext = self.external_links()
        return round(self.word_count / len(ext), 1) if ext else None

    def words_per_item(self) -> Optional[float]:
        """Sharper than words_per_link where a section is a list of items.

        A block averaging 35 words/link may be six tight one-line items or two
        sprawling paragraphs; dividing by list items separates those.
        """
        if not self.list_items:
            return None
        return round(self.word_count / self.list_items, 1)

    def heading_link_rate(self) -> Optional[float]:
        ext = self.external_links()
        if not ext:
            return None
        return round(sum(1 for link in ext if link.on_heading) / len(ext), 2)

    def domain_match_rate(self) -> Optional[float]:
        ext = self.external_links()
        if not ext:
            return None
        return round(sum(1 for link in ext if link.anchor_matches_domain) / len(ext), 2)


@dataclass
class Issue:
    url: str
    newsletter: str
    title: str
    meta: Dict[str, str] = field(default_factory=dict)
    blocks: List[Block] = field(default_factory=list)
    # Set when >=80% of external links share one domain — a click-tracking
    # redirect (r.smartbrief.com, link.mail.beehiiv.com). domain% and source
    # attribution are meaningless for that issue until redirects are resolved.
    tracked_link_domain: Optional[str] = None


# --------------------------------------------------------------------------
# fetch + parse
# --------------------------------------------------------------------------


def fetch(url: str, timeout: int = 30, use_cache: bool = True) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()[:20]
    cached = CACHE_DIR / f"{key}.html"

    if use_cache and cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")

    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    resp.raise_for_status()
    # requests guesses ISO-8859-1 when the server omits a charset, which
    # mangles emoji-heavy newsletters into mojibake. Trust the body instead.
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    cached.write_text(resp.text, encoding="utf-8")
    return resp.text


def _domain(href: str) -> str:
    try:
        host = urlparse(href).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _content_root(soup: BeautifulSoup) -> Tag:
    """Best-effort content container: the element with the most text.

    Table-layout emails (SmartBrief) have `body > table` and no wrapping div,
    so `table` is a candidate too. And if the densest candidate still holds
    under half the page's text, the heuristic has picked a fragment — use body.
    """
    body = soup.body or soup
    body_len = len(_normalise(body.get_text(" "))) or 1

    best: Optional[Tag] = None
    best_len = 0
    for node in soup.find_all(["article", "main", "div", "table"], recursive=True):
        text_len = len(_normalise(node.get_text(" ")))
        if text_len > best_len and len(node.find_all("div", recursive=False)) < 40:
            best, best_len = node, text_len

    if best is None or best_len / body_len < 0.5:
        return body
    return best


def _anchor_matches_domain(text: str, domain: str) -> bool:
    if not text or not domain:
        return False
    stem = domain.split(".")[0].lower()
    return stem in text.lower().replace(" ", "")


def describe(
    url: str, newsletter: str = "", meta: Optional[Dict[str, str]] = None, **kw
) -> Issue:
    soup = BeautifulSoup(fetch(url, **kw), "html.parser")

    for junk in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        junk.decompose()

    title = _normalise(soup.title.get_text() if soup.title else "") or url
    root = _content_root(soup)

    issue = Issue(
        url=url,
        newsletter=newsletter or _domain(url),
        title=title,
        meta=meta or {},
    )
    current = Block(heading="(preamble)", heading_level=None, word_count=0)
    buffer: List[str] = []

    def flush() -> None:
        text = _normalise(" ".join(buffer))
        current.word_count = len(text.split())
        for link in current.links:
            if link.text and text:
                idx = text.lower().find(link.text.lower()[:40])
                link.position_in_block = (
                    round(idx / max(len(text), 1), 3) if idx >= 0 else None
                )
        if current.word_count or current.links or current.images:
            issue.blocks.append(current)

    text_containers = HEADING_TAGS + ["p", "li", "div", "td", "ul", "ol", "table"]
    scan = HEADING_TAGS + ["p", "li", "div", "td", "a", "img", "pre", "button"]

    for node in root.find_all(scan):
        name = node.name

        if name in HEADING_TAGS:
            flush()
            buffer = []
            if TRAILER_PATTERNS.match(_normalise(node.get_text())):
                current = Block(heading="(trailer)", heading_level=None, word_count=0)
                buffer = []
                break  # site chrome from here on — not the issue
            current = Block(
                heading=_normalise(node.get_text()) or "(untitled)",
                heading_level=name,
                word_count=0,
            )
            # A heading that IS a link — TLDR wraps <a><h3>, Beehiiv often
            # <h3><a>. Attach it to the block it titles, not the previous one.
            wrap = node.find_parent("a") or node.find("a")
            if wrap is not None:
                href = wrap.get("href") or ""
                if href.startswith("http"):
                    dom = _domain(href)
                    current.links.append(
                        Link(
                            text=current.heading,
                            href=href,
                            domain=dom,
                            anchor_matches_domain=_anchor_matches_domain(
                                current.heading, dom
                            ),
                            position_in_block=0.0,
                            on_heading=True,
                        )
                    )
            continue

        if name == "a":
            if node.find(HEADING_TAGS) or node.find_parent(HEADING_TAGS):
                continue  # already attached via the heading above
            href = node.get("href") or ""
            if not href.startswith("http"):
                continue
            text = _normalise(node.get_text())
            if not text:
                continue
            dom = _domain(href)
            current.links.append(
                Link(
                    text=text,
                    href=href,
                    domain=dom,
                    anchor_matches_domain=_anchor_matches_domain(text, dom),
                    position_in_block=None,
                )
            )
        elif name == "img":
            current.images += 1
        elif name == "pre":
            current.code_blocks += 1
        elif name == "button":
            current.buttons += 1
        elif name == "li":
            current.list_items += 1
            buffer.append(node.get_text(" "))
        elif name == "p":
            if node.find_parent("li") is None:  # li already captured it
                buffer.append(node.get_text(" "))
        elif name in ("div", "td"):
            # Only leaf cells/divs. TLDR puts body copy in <div class="newsletter-html">
            # with no <p>; table-layout emails (SmartBrief) put everything in <td>
            # with no <p> at all. A container with block children would
            # double-count them.
            if (
                node.find(text_containers) is None
                and node.find_parent(["p", "li"]) is None
            ):
                buffer.append(node.get_text(" "))

    flush()

    domains = Counter(link.domain for b in issue.blocks for link in b.external_links())
    if domains:
        top, n = domains.most_common(1)[0]
        if n >= 5 and n / sum(domains.values()) >= 0.8:
            issue.tracked_link_domain = top
            print(
                f"!! {issue.newsletter}: {n}/{sum(domains.values())} links go via "
                f"{top} — tracked redirects, domain% unreliable",
                file=sys.stderr,
            )
    return issue


# --------------------------------------------------------------------------
# reports
# --------------------------------------------------------------------------


def print_issue(issue: Issue) -> None:
    print(f"\n{'=' * 78}\n[{issue.newsletter}] {issue.title}\n{issue.url}\n{'=' * 78}")
    for i, b in enumerate(issue.blocks, 1):
        ext = b.external_links()
        print(
            f"\n{i:2}. [{b.heading_level or '--'}] {b.heading[:70]}"
            f"\n    words={b.word_count:<5} links={len(ext):<3} "
            f"items={b.list_items:<3} img={b.images} code={b.code_blocks}"
        )
        for link in ext[:8]:
            pos = (
                "?"
                if link.position_in_block is None
                else f"{link.position_in_block:.2f}"
            )
            flag = " [anchor~domain]" if link.anchor_matches_domain else ""
            print(f"      → {link.text[:52]:<52} @{pos} {link.domain}{flag}")


def print_stability(newsletter: str, issues: List[Issue]) -> None:
    """Which slots recur across issues of ONE newsletter."""
    if len(issues) < 2:
        return

    print(f"\n\n{'=' * 78}")
    print(f"STABILITY — {newsletter} ({len(issues)} issues)")
    print("=" * 78)

    seen: Counter = Counter()
    positions: Dict[str, List[int]] = defaultdict(list)
    link_counts: Dict[str, List[int]] = defaultdict(list)

    for issue in issues:
        for idx, b in enumerate(issue.blocks):
            key = b.heading[:58]
            seen[key] += 1
            positions[key].append(idx)
            link_counts[key].append(len(b.external_links()))

    def span(values: List[int]) -> str:
        lo, hi = min(values), max(values)
        return str(lo) if lo == hi else f"{lo}-{hi}"

    print(f"\n{'heading':<58} {'seen':>4} {'pos':>7} {'links':>7}")
    print("-" * 78)
    recurring = 0
    for key, n in seen.most_common():
        if n < 2:
            continue
        recurring += 1
        print(f"{key:<58} {n:>4} {span(positions[key]):>7} {span(link_counts[key]):>7}")

    # Story-headed newsletters (TLDR, Carbon Brief) give every story its own
    # heading, so verbatim recurrence sees only noise. Their skeleton shows up
    # as a per-issue *shape*: runs of single-link headings are sections, and
    # if the run lengths line up across issues, structure is stable even with
    # zero recurring heading text.
    print(
        "\nShape per issue — N = run of N single-link headings, • = multi-link block, - = no links:"
    )
    for issue in issues:
        sig = []
        for b in _merge_single_link_runs(issue.blocks):
            n = len(b.external_links())
            if b.heading.startswith("[run of "):
                sig.append(str(b.list_items))
            elif n >= 2:
                sig.append("•")
            else:
                sig.append("-")
        print(f"  {' '.join(sig):<44} {issue.title[:32]}")

    once = [k for k, n in seen.items() if n == 1]
    print(
        f"\n{recurring} heading(s) recur verbatim; {len(once)} appear once."
        "\nNOTE: headings that appear once are often the SAME slot with variable"
        "\ntext (sponsor blocks, 'AI Skill of the Day: <topic>', weekday features)."
        "\nCheck them for pattern-matched slots before concluding structure is unstable:"
    )
    for k in once[:20]:
        print(f"  - {k}")
    if len(once) > 20:
        print(f"  ... and {len(once) - 20} more")


def _merge_single_link_runs(blocks: List[Block]) -> List[Block]:
    """Fold runs of one-link-per-heading blocks into a single pseudo-section.

    TLDR-style digests give every story its own <h3> with the headline as the
    link. Per-heading, each is a single-link block and carries no style signal;
    as a run they are the clearest headline-is-the-link section in the corpus.
    Heading LEVEL cannot be used to find sections — neuron uses h1, Daily
    Upside h2, Rundown h3/h4 — so this merges on structure instead: 2+ adjacent
    blocks at the same level, each with exactly one external link.
    """
    out: List[Block] = []
    run: List[Block] = []

    def flush_run() -> None:
        if len(run) >= 2:
            merged = Block(
                heading=f"[run of {len(run)} ×{run[0].heading_level}] {run[0].heading[:22]}…",
                heading_level=run[0].heading_level,
                word_count=sum(b.word_count for b in run),
                links=[link for b in run for link in b.links],
                images=sum(b.images for b in run),
                buttons=sum(b.buttons for b in run),
                code_blocks=sum(b.code_blocks for b in run),
                list_items=len(run),  # each story is one item
            )
            out.append(merged)
        else:
            out.extend(run)
        run.clear()

    for b in blocks:
        single = len(b.external_links()) == 1
        if single and (not run or run[-1].heading_level == b.heading_level):
            run.append(b)
        else:
            flush_run()
            if single:
                run.append(b)
            else:
                out.append(b)
    flush_run()
    return out


def print_style_signals(issues: List[Issue]) -> None:
    """Corpus-wide: the numbers the item-style taxonomy should be derived from.

    Sorted by words-per-link so clusters are visible. Prose-heavy sections with
    source-name anchoring sit at one end; bare link lists at the other. Whether
    that is four clusters or six is exactly what this table is for.
    """
    rows = []
    for issue in issues:
        for b in _merge_single_link_runs(issue.blocks):
            wpl = b.words_per_link()
            if wpl is None or len(b.external_links()) < 2:
                continue  # single-link blocks carry no style signal
            if b.word_count == 0:
                # Links with no prose are navigation — related-post cards,
                # "you might also like" footers — not an item style.
                continue
            rows.append(
                (
                    wpl,
                    b.words_per_item(),
                    b.domain_match_rate(),
                    b.heading_link_rate(),
                    len(b.external_links()),
                    b.list_items,
                    issue.newsletter,
                    b.heading[:34],
                )
            )

    if not rows:
        return

    rows.sort(key=lambda r: r[0])

    print(f"\n\n{'=' * 78}\nCORPUS STYLE SIGNALS ({len(rows)} multi-link sections)")
    print("=" * 78)
    print("\nlow words/link = bare link list │ high = prose with embedded links")
    print("domain% = links anchored on the publication name")
    print("head%   = links that ARE the heading (headline-is-the-link style)\n")
    print(
        f"{'w/link':>7} {'w/item':>7} {'domain%':>8} {'head%':>6} {'links':>6} "
        f"{'items':>6}  {'newsletter':<16} heading"
    )
    print("-" * 84)
    for wpl, wpi, dmr, hlr, nlinks, items, nl, heading in rows:
        dstr = "-" if dmr is None else f"{dmr:.0%}"
        hstr = "-" if hlr is None else f"{hlr:.0%}"
        istr = "-" if wpi is None else f"{wpi:.1f}"
        print(
            f"{wpl:>7.1f} {istr:>7} {dstr:>8} {hstr:>6} {nlinks:>6} {items:>6}  "
            f"{nl:<16} {heading}"
        )


# --------------------------------------------------------------------------
# input
# --------------------------------------------------------------------------


def load_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        # A line starting with '#' is a comment regardless of which column
        # the '#' lands in after splitting — strip them before the CSV parser.
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]

    reader = csv.DictReader(lines)
    if not reader.fieldnames or "issue_url" not in reader.fieldnames:
        raise SystemExit(f"{path}: CSV needs at least an 'issue_url' column")

    rows = []
    for row in reader:
        url = (row.get("issue_url") or "").strip()
        if not url:
            continue
        rows.append({k: (v or "").strip() for k, v in row.items()})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("urls", nargs="*", help="Newsletter issue URLs")
    ap.add_argument("--csv", help="CSV corpus (see module docstring for columns)")
    ap.add_argument("--urls-file", help="File with one issue URL per line")
    ap.add_argument("--json", dest="json_out", help="Write raw observations here")
    ap.add_argument("--no-cache", action="store_true", help="Bypass the fetch cache")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds between fetches")
    ap.add_argument("--quiet", action="store_true", help="Skip per-issue detail")
    args = ap.parse_args()

    targets: List[Dict[str, str]] = []
    for url in args.urls:
        targets.append({"issue_url": url, "newsletter": ""})
    if args.urls_file:
        with open(args.urls_file) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    targets.append({"issue_url": line, "newsletter": ""})
    if args.csv:
        targets += load_csv(args.csv)

    if not targets:
        ap.error("give at least one URL, --urls-file, or --csv")

    issues: List[Issue] = []
    for i, row in enumerate(targets):
        url = row["issue_url"]
        meta = {
            k: v for k, v in row.items() if k not in ("issue_url", "newsletter") and v
        }
        try:
            issue = describe(
                url,
                newsletter=row.get("newsletter", ""),
                meta=meta,
                use_cache=not args.no_cache,
            )
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"!! {url}: {exc}", file=sys.stderr)
            continue

        issues.append(issue)
        if not args.quiet:
            print_issue(issue)
        if args.delay and i < len(targets) - 1:
            time.sleep(args.delay)

    by_newsletter: Dict[str, List[Issue]] = defaultdict(list)
    for issue in issues:
        by_newsletter[issue.newsletter].append(issue)

    for newsletter, group in sorted(by_newsletter.items()):
        print_stability(newsletter, group)

    print_style_signals(issues)

    print(
        f"\n\nDescribed {len(issues)} issue(s) across "
        f"{len(by_newsletter)} newsletter(s)."
    )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps([asdict(i) for i in issues], indent=2), encoding="utf-8"
        )
        print(f"Raw observations → {args.json_out}")

    return 0 if issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
