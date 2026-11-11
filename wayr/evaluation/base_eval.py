"""Base evaluator for all WAYR agent evaluations."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import HttpUrl

from wayr.models import RSSArticle, ClassifiedArticle, GeneratedArticle
from wayr.models.articles import ContentType
from wayr.utils import get_logger

logger = get_logger(__name__)

# Banned adjectives shared across author and proofreader evals
BANNED_ADJECTIVES = [
    "groundbreaking",
    "revolutionary",
    "visionary",
    "game-changing",
    "ambitious",
    "bold",
    "daring",
    "legendary",
    "iconic",
    "innovative",
]


class BaseEvaluator:
    """Base class for all agent evaluators."""

    def __init__(self, agent_name: str, history_file: str):
        self.agent_name = agent_name
        self.history_file = Path(history_file)

    def _load_test_cases(self, test_cases_file: str) -> List[Dict]:
        """Load test cases from JSON file."""
        file_path = Path(test_cases_file)
        if not file_path.exists():
            raise FileNotFoundError(f"Test cases file not found: {test_cases_file}")

        with open(file_path, "r") as f:
            data = json.load(f)

        return data.get("test_cases", [])

    def _save_failed_cases(self, agent_name: str, failed: List[Dict]):
        """Save failed cases to JSON for review. Co-located with the evaluator's history file."""
        if not failed:
            return
        output_file = self.history_file.parent / f"failed_{agent_name}_cases.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            json.dump(failed, f, indent=2)
        logger.info(f"Saved {len(failed)} failed cases to {output_file}")

    def usage_summary(self) -> Optional[Dict]:
        """Tokens and cost of the run, summed over every agent the evaluator
        holds (whatever the attribute is called) — the number that makes two
        models comparable. A per-token price means nothing if a model spends
        twice the tokens on reasoning."""
        from wayr.agents.base import BaseAgent
        from wayr.utils.pricing import estimate_cost_usd

        agents = [
            v for v in vars(self).values() if isinstance(v, BaseAgent) and v.usage.calls
        ]
        if not agents:
            return None
        d: Dict = {
            "calls": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
        }
        cost: Optional[float] = 0.0
        for a in agents:
            for k in d:
                d[k] += getattr(a.usage, k)
            c = estimate_cost_usd(
                a.model,
                a.usage.input_tokens,
                a.usage.output_tokens,
                a.usage.cached_input_tokens,
            )
            cost = None if (cost is None or c is None) else cost + c
        d["cost_usd"] = cost
        d["cost_per_call_usd"] = cost / d["calls"] if cost is not None else None
        return d

    def _save_history(self, run_summary: Dict):
        """Save run summary to agent-specific history file. Keep last 10 runs."""
        usage = self.usage_summary()
        if usage is not None:
            run_summary = {**run_summary, "usage": usage}
            cost = (
                f"${usage['cost_usd']:.4f} (${usage['cost_per_call_usd'] * 1000:.2f} per 1k calls)"
                if usage["cost_usd"] is not None
                else "unknown price"
            )
            logger.info(
                f"Usage: {usage['calls']} calls · {usage['input_tokens']:,} in "
                f"({usage['cached_input_tokens']:,} cached) · {usage['output_tokens']:,} out "
                f"({usage['reasoning_tokens']:,} reasoning) · cost {cost}"
            )
        history = []
        if self.history_file.exists():
            with open(self.history_file, "r") as f:
                history = json.load(f)

        history.append(run_summary)
        if len(history) > 10:
            history = history[-10:]

        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "w") as f:
            json.dump(history, f, indent=2)

        logger.info(f"Saved evaluation history to {self.history_file}")

    # --- Shared HTML/text utilities ---

    @staticmethod
    def strip_html(html_content: str) -> str:
        """Strip HTML tags and return plain text."""
        text = re.sub(r"<[^>]+>", "", html_content)
        import html

        return html.unescape(text).strip()

    @staticmethod
    def count_words(html_content: str) -> int:
        """Count words in HTML content after stripping tags."""
        text = BaseEvaluator.strip_html(html_content)
        return len(text.split())

    @staticmethod
    def count_h3_tags(html_content: str) -> int:
        """Count <h3> subheadings in HTML content."""
        return len(re.findall(r"<h3[^>]*>", html_content, re.IGNORECASE))

    @staticmethod
    def find_banned_adjectives(
        text: str, adjectives: Optional[List[str]] = None
    ) -> List[str]:
        """Find banned adjectives in text using word-boundary regex."""
        if adjectives is None:
            adjectives = BANNED_ADJECTIVES
        text_lower = text.lower()
        found = []
        for adj in adjectives:
            if re.search(rf"\b{re.escape(adj)}\b", text_lower):
                found.append(adj)
        return found

    @staticmethod
    def has_source_citation(
        html_content: str, expected_url: Optional[str] = None
    ) -> bool:
        """Check if content has a source citation link."""
        if expected_url:
            return expected_url in html_content
        # Check for any Source: <a href="..."> pattern
        return bool(re.search(r"<strong>Source:</strong>.*?<a\s+href=", html_content))

    @staticmethod
    def has_ai_disclosure(html_content: str) -> bool:
        """Check if AI disclosure div is present."""
        return "ai-disclosure" in html_content

    # --- Model construction helpers ---

    @staticmethod
    def build_rss_article(source: Dict) -> RSSArticle:
        """Build an RSSArticle from test case source data."""
        published_date = source.get("published_date")
        if isinstance(published_date, str):
            from dateutil.parser import parse

            published_date = parse(published_date)
        elif published_date is None:
            published_date = datetime.now(timezone.utc)

        return RSSArticle(
            title=source["title"],
            url=HttpUrl(source["url"]),
            source=source.get("source", "Test Source"),
            published_date=published_date,
            summary=source.get("summary"),
            content=source.get("content"),
            tags=source.get("tags", []),
            source_origin=source.get("source_origin"),
        )

    @staticmethod
    def build_classified_article(source: Dict) -> ClassifiedArticle:
        """Build a ClassifiedArticle from test case source data."""
        rss = BaseEvaluator.build_rss_article(source)

        # Parse categories — strings now, no enum validation
        categories_raw = source.get("categories", [source.get("category", "General")])
        categories = [str(c) for c in categories_raw if c]

        # Map legacy content types (e.g. "guide") for test compatibility
        raw_content_type = source.get("content_type", "news")
        legacy_map = {"guide": "other"}
        raw_content_type = legacy_map.get(raw_content_type, raw_content_type)

        return ClassifiedArticle(
            original=rss,
            belongs_to_industry=source.get("belongs_to_industry", True),
            content_type=ContentType(raw_content_type),
            content_type_confidence=source.get("content_type_confidence", 0.95),
            content_type_reason=source.get("content_type_reason", "Test case"),
            categories=categories,
            category_confidences=source.get(
                "category_confidences", [0.95] * len(categories)
            ),
            confidence=source.get("confidence", 0.95),
            classification_reason=source.get("classification_reason", "Test case"),
        )

    @staticmethod
    def build_generated_article(input_data: Dict) -> GeneratedArticle:
        """Build a GeneratedArticle from test case input data."""
        # Build the nested chain: RSSArticle -> ClassifiedArticle
        source_data = {
            "title": input_data.get("original_title", "Test Article"),
            "url": input_data.get("original_url", "https://example.com/test"),
            "source": input_data.get("original_source", "Test Source"),
            "summary": input_data.get("original_summary"),
            "content": input_data.get("original_content"),
            "tags": input_data.get("original_tags", []),
            "categories": input_data.get("categories", ["General"]),
        }
        classified = BaseEvaluator.build_classified_article(source_data)

        return GeneratedArticle(
            classified=classified,
            generated_title=input_data["generated_title"],
            generated_content=input_data["generated_content"],
            sources_cited=input_data.get("sources_cited", []),
            generated_at=datetime.now(timezone.utc),
            model_used="test",
        )
