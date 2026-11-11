"""Append test cases to an evaluator's test_cases.json from ``pipeline_articles``.

Given a list of url_hashes from Supabase's ``pipeline_articles`` table, fetch
each article's title/url/source/raw_content and append it as a test case.

Used for curating real-world failure cases observed in production runs into
the eval suite (e.g., Tesla rejections from run02).

Edit TARGETS below to add more cases, then:
    poetry run python scripts/add_pipeline_articles_as_test_cases.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import List, Optional

from supabase import create_client

from wayr.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


TARGETS = [
    # --- industry_classifier ---
    # Tesla Q1 revenue — clearest EV/automotive case (run02 correctly rejected)
    {
        "url_hash": "047eb7a6e03b85b9",
        "eval": "industry_classifier",
        "belongs_to_industry": False,
        "detected_industries": ["automotive"],
        "description_suffix": "[US, non_software_tech]",
    },
    # SpaceX acquires Cursor (AI coding software) — headline says SpaceX but
    # the article's subject is a software M&A. Should be belongs=true.
    {
        "url_hash": "cdb791e266a7f399",
        "eval": "industry_classifier",
        "belongs_to_industry": True,
        "detected_industries": ["software technology", "ai"],
        "description_suffix": "[US, software_tech]",
    },
    # ARK/Cathie Wood invests in Lucra (software startup). Run02 wrongly
    # rejected because headline says "isn't AI" + VC-heavy framing. Should
    # be belongs=true.
    {
        "url_hash": "2502eb6361befeef",
        "eval": "industry_classifier",
        "belongs_to_industry": True,
        "detected_industries": ["software technology"],
        "description_suffix": "[US, software_tech]",
    },
    # --- news_classifier ---
    # SpaceX/Cursor buyout — straight M&A news. Run02 failed to parse and
    # defaulted to OTHER; expected = news.
    {
        "url_hash": "cdb791e266a7f399",
        "eval": "news_classifier",
        "content_type": "news",
        "description_suffix": "[news]",
    },
    # Elizabeth Warren AI crisis warning — reported-opinion (Warren's argument
    # at a Verge event). Run02 classified as news and published; expected =
    # opinion.
    {
        "url_hash": "0c7fe32ed0e9bb21",
        "eval": "news_classifier",
        "content_type": "opinion",
        "description_suffix": "[opinion]",
    },
    # Microsoft voluntary buyout program — concrete confirmed corporate event
    # (first in company's 51-year history). Run04 wrongly classified as
    # non-news/other; expected = news.
    {
        "url_hash": "3662bc14f7bff412",
        "eval": "news_classifier",
        "content_type": "news",
        "description_suffix": "[news]",
    },
    # NK hackers using AI tools — consolidation/analysis of multiple events,
    # editorial framing rather than reporting a single new announcement.
    # Run02/04 classified as news; expected = opinion.
    {
        "url_hash": "098267d44a38ce1c",
        "eval": "news_classifier",
        "content_type": "opinion",
        "description_suffix": "[opinion]",
    },
]


def _short_id(url: str) -> str:
    return "real_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def _short_title(title: str, n: int = 60) -> str:
    t = title.strip()
    return t if len(t) <= n else t[: n - 1] + "…"


def _fetch_articles(url_hashes: List[str]) -> List[dict]:
    s = get_settings()
    if not s.supabase_url or not s.supabase_secret_key:
        print(
            "ERROR: SUPABASE_URL and SUPABASE_SECRET_KEY must be set", file=sys.stderr
        )
        sys.exit(1)
    client = create_client(s.supabase_url, s.supabase_secret_key)
    res = (
        client.table("pipeline_articles")
        .select("url_hash,title,source_url,source_tag,raw_content")
        .in_("url_hash", url_hashes)
        .execute()
    )
    return res.data or []


def _test_cases_path(eval_name: str) -> Path:
    return REPO_ROOT / f"wayr/evaluation/{eval_name}/test_cases.json"


def _load_cases(path: Path) -> List[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())["test_cases"]


def _save_cases(path: Path, cases: List[dict]) -> None:
    cases = sorted(cases, key=lambda c: c["id"])
    path.write_text(
        json.dumps({"test_cases": cases}, indent=2, ensure_ascii=False) + "\n"
    )


def _build_source_block(row: dict) -> dict:
    return {
        "title": row.get("title") or "",
        "url": row.get("source_url") or "",
        "source": row.get("source_tag") or "Unknown",
        "content": row.get("raw_content") or "",
    }


def _build_description(row: dict, target: dict) -> str:
    return (
        f"{row.get('source_tag') or 'unknown'} — "
        f"{_short_title(row.get('title') or '')} "
        f"{target.get('description_suffix') or ''}"
    ).strip()


def _build_industry_case(row: dict, target: dict) -> Optional[dict]:
    url = row.get("source_url") or ""
    if not url:
        return None
    case = {
        "id": _short_id(url),
        "description": _build_description(row, target),
        "source": _build_source_block(row),
        "expected": {
            "belongs_to_industry": bool(target["belongs_to_industry"]),
        },
    }
    if target.get("detected_industries"):
        case["expected"]["detected_industries"] = target["detected_industries"]
    return case


def _build_news_case(row: dict, target: dict) -> Optional[dict]:
    url = row.get("source_url") or ""
    if not url:
        return None
    return {
        "id": _short_id(url),
        "description": _build_description(row, target),
        "source": _build_source_block(row),
        "expected": {
            "content_type": str(target["content_type"]).lower(),
        },
    }


def main() -> int:
    by_eval: dict[str, list[dict]] = {}
    for t in TARGETS:
        by_eval.setdefault(t["eval"], []).append(t)

    all_hashes = [t["url_hash"] for t in TARGETS]
    rows = _fetch_articles(all_hashes)
    rows_by_hash = {r["url_hash"]: r for r in rows}

    missing = [h for h in all_hashes if h not in rows_by_hash]
    if missing:
        print(f"WARNING: url_hashes not found in pipeline_articles: {missing}")

    for eval_name, targets in by_eval.items():
        path = _test_cases_path(eval_name)
        cases = _load_cases(path)
        existing_ids = {c["id"] for c in cases}

        added = 0
        skipped = 0
        for t in targets:
            row = rows_by_hash.get(t["url_hash"])
            if not row:
                continue
            if not (row.get("raw_content") or "").strip():
                print(f"  skip (no raw_content): {t['url_hash']} | {row.get('title')}")
                continue
            if eval_name == "industry_classifier":
                case = _build_industry_case(row, t)
            elif eval_name == "news_classifier":
                case = _build_news_case(row, t)
            else:
                print(f"  unsupported eval: {eval_name}")
                continue
            if not case:
                continue
            if case["id"] in existing_ids:
                print(f"  skip (exists): {case['id']} | {row.get('title')}")
                skipped += 1
                continue
            cases.append(case)
            existing_ids.add(case["id"])
            added += 1
            print(f"  add: {case['id']} | {row.get('title')}")

        if added:
            _save_cases(path, cases)
            print(
                f"[{eval_name}] wrote {len(cases)} total cases (+{added} new, {skipped} already present)"
            )
        else:
            print(f"[{eval_name}] no new cases added ({skipped} already present)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
