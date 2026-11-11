"""Build real-world classifier test cases from Supabase labeled data.

Pulls manually-labeled rows from ``post_records`` joined with
``source_documents.clean_text`` and curates a stratified 50-case eval set for
each of the two classifiers:

- ``wayr/evaluation/industry_classifier/test_cases.json``
- ``wayr/evaluation/news_classifier/test_cases.json``

Deterministic: sampling sorts by ``source_url`` within each stratum so repeat
runs against the same DB state produce byte-identical JSON.

Requires ``SUPABASE_URL`` and ``SUPABASE_SECRET_KEY`` in ``.env``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from supabase import Client, create_client

from wayr.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_classifier_test_cases")

REPO_ROOT = Path(__file__).resolve().parents[1]
INDUSTRY_TEST_CASES = REPO_ROOT / "wayr/evaluation/industry_classifier/test_cases.json"
NEWS_TEST_CASES = REPO_ROOT / "wayr/evaluation/news_classifier/test_cases.json"
INDUSTRY_WORKBENCH_CSV = REPO_ROOT / "wayr/evaluation/industry_classifier/workbench.csv"
NEWS_WORKBENCH_CSV = REPO_ROOT / "wayr/evaluation/news_classifier/workbench.csv"

INDUSTRY_TARGET = 50
NEWS_TARGET = 50

NON_NEWS_SUB_QUOTA = {
    "review": 5,
    "opinion": 5,
    "deal": 5,
    "report": 5,
    "other": 5,
}

# Map reviewer-invented non-news labels onto the agent's enum so expected
# content_type values match what the classifier can actually output.
NON_NEWS_LABEL_ALIASES = {
    "promo": "deal",
    "roundup": "other",
}

PAGE_SIZE = 1000


def _client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_secret_key:
        logger.error("SUPABASE_URL and SUPABASE_SECRET_KEY must be set in .env")
        sys.exit(1)
    return create_client(settings.supabase_url, settings.supabase_secret_key)


def _fetch_all(
    client: Client,
    table: str,
    *,
    select: str = "*",
    filter_fn: Optional[Callable[[Any], Any]] = None,
) -> List[Dict]:
    rows: List[Dict] = []
    offset = 0
    while True:
        q = client.table(table).select(select).range(offset, offset + PAGE_SIZE - 1)
        if filter_fn:
            q = filter_fn(q)
        result = q.execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def _fetch_labeled_posts(client: Client) -> List[Dict]:
    def _filter(q):
        # Rows that have at least one manual label relevant to our two evals.
        return q.or_("is_software_industry.not.is.null,news_or_non_news.not.is.null")

    rows = _fetch_all(client, "post_records", filter_fn=_filter)
    logger.info("Fetched %d labeled post_records", len(rows))
    return rows


def _fetch_source_docs(client: Client, urls: List[str]) -> Dict[str, str]:
    """Fetch clean_text for the given source_urls. Returns {url: clean_text}."""
    out: Dict[str, str] = {}
    # PostgREST .in_ has a URL-length cap; chunk the lookup.
    chunk = 100
    for i in range(0, len(urls), chunk):
        slice_urls = urls[i : i + chunk]
        result = (
            client.table("source_documents")
            .select("source_url,clean_text")
            .in_("source_url", slice_urls)
            .execute()
        )
        for row in result.data or []:
            if row.get("clean_text"):
                out[row["source_url"]] = row["clean_text"]
    logger.info("Fetched clean_text for %d / %d source URLs", len(out), len(urls))
    return out


def _short_id(url: str) -> str:
    return "real_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def _short_title(title: Optional[str], n: int = 60) -> str:
    t = (title or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _geo(row: Dict) -> str:
    """Best-effort US vs IN tag from source_xml_file / publication."""
    hints = " ".join(
        str(row.get(k) or "") for k in ("source_xml_file", "publication")
    ).lower()
    if "india" in hints or "_in_" in hints or "-in-" in hints or "/in/" in hints:
        return "IN"
    return "US"


def _build_source(row: Dict, clean_text: str) -> Dict:
    """Test case source block — only fields the eval/agent actually use."""
    return {
        "title": row.get("title") or "",
        "url": row.get("source_url") or "",
        "source": row.get("publication") or "Unknown",
        "content": clean_text,
    }


def _parse_expected_industries(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [
        part.strip().lower() for part in str(raw).split(",") if part and part.strip()
    ]


def _stable_sort_key(row: Dict) -> str:
    return row.get("source_url") or ""


def _edge_case_score(row: Dict) -> int:
    """Higher = prefer sampling this row (explicit edge case / reviewer comment)."""
    score = 0
    if row.get("edge_case_flag"):
        score += 2
    if row.get("reviewer_comment"):
        score += 1
    return score


def _take(pool: List[Dict], n: int, *, prefer_edge: bool = False) -> List[Dict]:
    if n <= 0 or not pool:
        return []
    if prefer_edge:
        # Stable sort: edge-case score desc, then by source_url asc for determinism.
        pool = sorted(pool, key=lambda r: (-_edge_case_score(r), _stable_sort_key(r)))
    else:
        pool = sorted(pool, key=_stable_sort_key)
    return pool[:n]


def _curate_industry(posts: List[Dict]) -> List[Dict]:
    """Stratify by is_software_industry × geo, 50 total."""
    buckets: Dict[tuple, List[Dict]] = defaultdict(list)
    for row in posts:
        label = (row.get("is_software_industry") or "").strip().lower()
        if label not in {"software_tech", "non_software_tech"}:
            continue
        buckets[(label, _geo(row))].append(row)

    quotas = {
        ("software_tech", "US"): 15,
        ("software_tech", "IN"): 10,
        ("non_software_tech", "US"): 10,
        ("non_software_tech", "IN"): 10,
    }
    # Reserve 5 slots for explicit edge cases across any bucket.
    edge_quota = 5

    sampled: List[Dict] = []
    seen_urls: set[str] = set()

    for key, n in quotas.items():
        picked = _take(buckets.get(key, []), n, prefer_edge=True)
        for row in picked:
            if row["source_url"] not in seen_urls:
                sampled.append(row)
                seen_urls.add(row["source_url"])
        logger.info(
            "industry bucket %s: picked %d / target %d (pool=%d)",
            key,
            len(picked),
            n,
            len(buckets.get(key, [])),
        )

    edge_pool = [
        r
        for r in posts
        if r.get("source_url") not in seen_urls and _edge_case_score(r) > 0
    ]
    edge_picks = _take(edge_pool, edge_quota, prefer_edge=True)
    for row in edge_picks:
        sampled.append(row)
        seen_urls.add(row["source_url"])
    logger.info(
        "industry edge-case slots: picked %d / target %d (pool=%d)",
        len(edge_picks),
        edge_quota,
        len(edge_pool),
    )

    # If we underfilled, top up from the largest remaining bucket deterministically.
    if len(sampled) < INDUSTRY_TARGET:
        deficit = INDUSTRY_TARGET - len(sampled)
        remaining = [r for r in posts if r["source_url"] not in seen_urls]
        topups = _take(remaining, deficit)
        sampled.extend(topups)
        logger.info("industry top-up: added %d rows to reach target", len(topups))

    return sampled


def _curate_news(posts: List[Dict]) -> List[Dict]:
    """Stratify by news_or_non_news × non_news_category, 50 total, software_tech only."""
    software_only = [
        r
        for r in posts
        if (r.get("is_software_industry") or "").strip().lower() == "software_tech"
        and (r.get("news_or_non_news") or "").strip()
    ]
    logger.info("news pool (software_tech w/ label): %d rows", len(software_only))

    news_pool = [
        r
        for r in software_only
        if (r.get("news_or_non_news") or "").strip().lower() == "news"
    ]
    non_news_pool_by_sub: Dict[str, List[Dict]] = defaultdict(list)
    for r in software_only:
        label = (r.get("news_or_non_news") or "").strip().lower()
        if label == "news":
            continue
        sub = (r.get("non_news_category") or "other").strip().lower()
        sub = NON_NEWS_LABEL_ALIASES.get(sub, sub)
        non_news_pool_by_sub[sub].append(r)

    sampled: List[Dict] = []
    seen_urls: set[str] = set()

    news_picks = _take(news_pool, 25, prefer_edge=True)
    for row in news_picks:
        sampled.append(row)
        seen_urls.add(row["source_url"])
    logger.info(
        "news NEWS bucket: picked %d / target 25 (pool=%d)",
        len(news_picks),
        len(news_pool),
    )

    for sub, quota in NON_NEWS_SUB_QUOTA.items():
        pool = non_news_pool_by_sub.get(sub, [])
        picked = _take(pool, quota, prefer_edge=True)
        for row in picked:
            if row["source_url"] not in seen_urls:
                sampled.append(row)
                seen_urls.add(row["source_url"])
        logger.info(
            "news non_news[%s]: picked %d / target %d (pool=%d)",
            sub,
            len(picked),
            quota,
            len(pool),
        )

    # Backfill any non-news shortfall from the largest remaining non-news bucket.
    target_non_news = sum(NON_NEWS_SUB_QUOTA.values())
    current_non_news = sum(
        1 for r in sampled if (r.get("news_or_non_news") or "").lower() != "news"
    )
    if current_non_news < target_non_news:
        deficit = target_non_news - current_non_news
        leftovers = []
        for pool in non_news_pool_by_sub.values():
            for row in pool:
                if row["source_url"] not in seen_urls:
                    leftovers.append(row)
        topups = _take(leftovers, deficit, prefer_edge=True)
        for row in topups:
            sampled.append(row)
            seen_urls.add(row["source_url"])
        logger.info("news non-news backfill: added %d rows", len(topups))

    return sampled


def _render_industry_case(row: Dict, clean_text: str) -> Dict:
    label = (row.get("is_software_industry") or "").strip().lower()
    belongs = label == "software_tech"
    case = {
        "id": _short_id(row["source_url"]),
        "description": (
            f"{row.get('publication') or 'unknown'} — "
            f"{_short_title(row.get('title'))} "
            f"[{_geo(row)}, {label or 'unlabeled'}]"
        ),
        "source": _build_source(row, clean_text),
        "expected": {
            "belongs_to_industry": belongs,
        },
    }
    expected_industries = _parse_expected_industries(row.get("industry_expected"))
    if expected_industries:
        case["expected"]["detected_industries"] = expected_industries
    return case


def _render_news_case(row: Dict, clean_text: str) -> Optional[Dict]:
    label = (row.get("news_or_non_news") or "").strip().lower()
    if label == "news":
        content_type = "news"
    elif label == "non_news":
        content_type = (row.get("non_news_category") or "other").strip().lower()
        content_type = NON_NEWS_LABEL_ALIASES.get(content_type, content_type)
    else:
        return None

    tag = content_type if content_type != "news" else "news"
    return {
        "id": _short_id(row["source_url"]),
        "description": (
            f"{row.get('publication') or 'unknown'} — "
            f"{_short_title(row.get('title'))} [{tag}]"
        ),
        "source": _build_source(row, clean_text),
        "expected": {
            "content_type": content_type,
        },
    }


def _write_cases(path: Path, cases: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cases = sorted(cases, key=lambda c: c["id"])
    payload = {"test_cases": cases}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    logger.info("Wrote %d cases → %s", len(cases), path)


def _write_industry_workbench_csv(path: Path, cases: List[Dict]) -> None:
    """OpenAI workbench import: Industry, article, expected_output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    industry = get_settings().industry
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Industry", "article", "expected_output"]
        )
        writer.writeheader()
        for case in sorted(cases, key=lambda c: c["id"]):
            writer.writerow(
                {
                    "Industry": industry,
                    "article": case["source"].get("content", ""),
                    "expected_output": (
                        "Yes" if case["expected"]["belongs_to_industry"] else "No"
                    ),
                }
            )
    logger.info("Wrote %d rows → %s", len(cases), path)


def _write_news_workbench_csv(path: Path, cases: List[Dict]) -> None:
    """OpenAI workbench import: article, expected_output (Yes = NEWS, No = not)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["article", "expected_output"])
        writer.writeheader()
        for case in sorted(cases, key=lambda c: c["id"]):
            writer.writerow(
                {
                    "article": case["source"].get("content", ""),
                    "expected_output": (
                        "Yes" if case["expected"]["content_type"] == "news" else "No"
                    ),
                }
            )
    logger.info("Wrote %d rows → %s", len(cases), path)


def _summarize_industry(cases: List[Dict]) -> None:
    belongs_counts = Counter(c["expected"]["belongs_to_industry"] for c in cases)
    logger.info(
        "industry summary: belongs=True %d, belongs=False %d",
        belongs_counts.get(True, 0),
        belongs_counts.get(False, 0),
    )


def _summarize_news(cases: List[Dict]) -> None:
    type_counts = Counter(c["expected"]["content_type"] for c in cases)
    logger.info("news summary: %s", dict(type_counts))


def main() -> int:
    client = _client()

    labeled = _fetch_labeled_posts(client)
    if not labeled:
        logger.error("No labeled rows found — nothing to do")
        return 1

    industry_pool = [r for r in labeled if r.get("is_software_industry")]
    news_pool = [r for r in labeled if r.get("news_or_non_news")]

    industry_sampled = _curate_industry(industry_pool)
    news_sampled = _curate_news(news_pool)

    # Resolve scraped content for every sampled row.
    all_urls = {r["source_url"] for r in industry_sampled} | {
        r["source_url"] for r in news_sampled
    }
    clean_by_url = _fetch_source_docs(client, sorted(all_urls))

    industry_cases: List[Dict] = []
    skipped_industry = 0
    for row in industry_sampled:
        ct = clean_by_url.get(row["source_url"])
        if not ct:
            skipped_industry += 1
            continue
        industry_cases.append(_render_industry_case(row, ct))

    news_cases: List[Dict] = []
    skipped_news = 0
    for row in news_sampled:
        ct = clean_by_url.get(row["source_url"])
        if not ct:
            skipped_news += 1
            continue
        rendered = _render_news_case(row, ct)
        if rendered is None:
            skipped_news += 1
            continue
        news_cases.append(rendered)

    if skipped_industry:
        logger.warning(
            "industry: skipped %d row(s) missing clean_text", skipped_industry
        )
    if skipped_news:
        logger.warning("news: skipped %d row(s) missing clean_text", skipped_news)

    _write_cases(INDUSTRY_TEST_CASES, industry_cases)
    _write_cases(NEWS_TEST_CASES, news_cases)
    _write_industry_workbench_csv(INDUSTRY_WORKBENCH_CSV, industry_cases)
    _write_news_workbench_csv(NEWS_WORKBENCH_CSV, news_cases)
    _summarize_industry(industry_cases)
    _summarize_news(news_cases)
    return 0


if __name__ == "__main__":
    sys.exit(main())
