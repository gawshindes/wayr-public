"""Build news_classifier test cases from the `eval_news_classifier` labels table.

Joins `eval_news_classifier` (labels) with `pipeline_articles` (content) to
produce a test_cases.json the eval can consume.

Label shape is binary (``expected_result`` boolean): True → "news",
False → "other". Subcategory nuance (deal/opinion/report/review) is not
tracked in this schema — if you need it, keep using the hand-tagged
test_cases.json instead.

Usage:
    poetry run python scripts/build_news_classifier_test_cases_from_db.py
    poetry run python scripts/build_news_classifier_test_cases_from_db.py --prompt-version 0.1
    poetry run python scripts/build_news_classifier_test_cases_from_db.py --output wayr/evaluation/news_classifier/test_cases.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from supabase import Client, create_client

from wayr.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_news_classifier_test_cases_from_db")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "wayr/evaluation/news_classifier/test_cases.from_db.json"


def _client() -> Client:
    s = get_settings()
    if not s.supabase_url or not s.supabase_secret_key:
        logger.error("SUPABASE_URL and SUPABASE_SECRET_KEY must be set in .env")
        sys.exit(1)
    return create_client(s.supabase_url, s.supabase_secret_key)


def _fetch_labels(client: Client, prompt_version: Optional[str]) -> List[Dict]:
    q = client.table("eval_news_classifier").select(
        "url_hash,prompt_version,expected_result,reviewer_comment,reviewed_at"
    )
    if prompt_version:
        q = q.eq("prompt_version", prompt_version)
    rows = q.execute().data or []
    logger.info(
        "Fetched %d label rows%s",
        len(rows),
        f" for prompt_version={prompt_version}" if prompt_version else "",
    )
    return rows


def _dedupe_latest_per_hash(rows: List[Dict]) -> List[Dict]:
    """If multiple prompt_versions exist for the same url_hash, keep the most
    recently-labeled row. Sort key prefers reviewed_at, falls back to
    prompt_version lexicographically."""

    def key(r: Dict) -> tuple:
        return (r.get("reviewed_at") or "", r.get("prompt_version") or "")

    latest: Dict[str, Dict] = {}
    for r in rows:
        uh = r["url_hash"]
        if uh not in latest or key(r) > key(latest[uh]):
            latest[uh] = r
    return list(latest.values())


def _fetch_articles(client: Client, url_hashes: List[str]) -> Dict[str, Dict]:
    out: Dict[str, Dict] = {}
    chunk = 100
    for i in range(0, len(url_hashes), chunk):
        slice_ = url_hashes[i : i + chunk]
        res = (
            client.table("pipeline_articles")
            .select("url_hash,title,source_url,source_tag,raw_content")
            .in_("url_hash", slice_)
            .execute()
        )
        for row in res.data or []:
            out[row["url_hash"]] = row
    return out


def _short_id(url: str) -> str:
    return "real_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def _short_title(title: str, n: int = 60) -> str:
    t = (title or "").strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _describe(row: Dict, label_row: Dict) -> str:
    reviewer_comment = (label_row.get("reviewer_comment") or "").strip()
    label = "news" if label_row["expected_result"] else "other"
    base = (
        f"{row.get('source_tag') or 'unknown'} — "
        f"{_short_title(row.get('title') or '')} [{label}]"
    )
    if reviewer_comment:
        return f"{base} — {reviewer_comment}"
    return base


def _build_case(label_row: Dict, article_row: Dict) -> Optional[Dict]:
    url = article_row.get("source_url") or ""
    content = article_row.get("raw_content") or ""
    if not url or not content.strip():
        return None
    return {
        "id": _short_id(url),
        "description": _describe(article_row, label_row),
        "source": {
            "title": article_row.get("title") or "",
            "url": url,
            "source": article_row.get("source_tag") or "Unknown",
            "content": content,
        },
        "expected": {
            "content_type": "news" if label_row["expected_result"] else "other",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt-version",
        help="Filter labels to a specific prompt_version. Omit to take latest per url_hash.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args()

    client = _client()
    labels = _fetch_labels(client, args.prompt_version)
    if not labels:
        logger.error("No label rows found — nothing to do")
        return 1

    if not args.prompt_version:
        labels = _dedupe_latest_per_hash(labels)

    article_map = _fetch_articles(client, [r["url_hash"] for r in labels])

    cases: List[Dict] = []
    skipped_missing_content = 0
    skipped_no_article = 0
    for label_row in labels:
        article = article_map.get(label_row["url_hash"])
        if not article:
            skipped_no_article += 1
            continue
        case = _build_case(label_row, article)
        if case is None:
            skipped_missing_content += 1
            continue
        cases.append(case)

    cases.sort(key=lambda c: c["id"])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"test_cases": cases}, indent=2, ensure_ascii=False) + "\n"
    )
    logger.info(
        "Wrote %d cases → %s (skipped: %d no-article, %d empty-content)",
        len(cases),
        args.output.relative_to(REPO_ROOT),
        skipped_no_article,
        skipped_missing_content,
    )

    from collections import Counter

    ct = Counter(c["expected"]["content_type"] for c in cases)
    logger.info("content_type distribution: %s", dict(ct))
    return 0


if __name__ == "__main__":
    sys.exit(main())
