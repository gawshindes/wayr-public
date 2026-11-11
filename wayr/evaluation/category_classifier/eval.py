"""Category classifier evaluation.

Evaluates CategoryClassifierAgent in isolation. Only runs on cases that would
reach the category classifier in production — i.e. where
`expected.content_type == "news"` and `expected.category` is set. Passing is
defined as the expected category appearing in {predicted_primary,
predicted_secondary}.
"""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from wayr.agents.category_classifier import CategoryClassifierAgent
from wayr.evaluation.base_eval import BaseEvaluator
from wayr.evaluation.metrics import (
    AgentEvaluationResult,
    CategoryClassifierAgentMetrics,
)
from wayr.models import RSSArticle
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"


class CategoryClassifierEvaluator(BaseEvaluator):
    """Evaluator for the category classifier (primary + secondary category assignment)."""

    def __init__(self, agent: CategoryClassifierAgent):
        super().__init__(
            agent_name="category_classifier",
            history_file=str(_HERE / "history.json"),
        )
        self.agent = agent

    async def evaluate_single(
        self, test_case: Dict
    ) -> Optional[CategoryClassifierAgentMetrics]:
        test_id = test_case.get("id", "unknown")
        expected = test_case.get("expected", {})

        # Only evaluate cases that would reach the category classifier in production.
        if expected.get("content_type") != "news":
            return None
        expected_category = expected.get("category")
        if not expected_category:
            return None
        expected_categories = expected.get("categories")

        article = self._build_article(test_case["source"])
        result = await self.agent.categorize(article)
        predicted_pair = [result.primary, result.secondary]

        metrics = CategoryClassifierAgentMetrics(
            test_case_id=test_id,
            article_title=article.title,
            predicted_primary=result.primary,
            predicted_secondary=result.secondary,
            expected_category=expected_category,
            expected_categories=expected_categories,
            reasoning=result.reasoning,
        )

        primary_exact = result.primary == expected_category
        metrics.checks["primary_exact"] = primary_exact

        in_pair = expected_category in predicted_pair
        metrics.checks["expected_in_predicted"] = in_pair

        if expected_categories:
            expected_set = set(expected_categories)
            predicted_set = set(predicted_pair)
            coverage = expected_set.issubset(predicted_set)
            metrics.coverage_correct = coverage
            metrics.checks["full_coverage"] = coverage
            if not coverage:
                missing = expected_set - predicted_set
                metrics.issues.append(f"Missing expected categories: {sorted(missing)}")

        if not primary_exact:
            metrics.issues.append(
                f"Primary mismatch: expected '{expected_category}', "
                f"got '{result.primary}' (secondary: '{result.secondary}')"
            )

        # Pass = the expected category appears anywhere in the predicted pair.
        # Returning the expected category as secondary is still correct for
        # WordPress routing.
        metrics.passed = in_pair
        return metrics

    async def run_test_suite(self, test_cases_file: str) -> AgentEvaluationResult:
        test_cases = self._load_test_cases(test_cases_file)
        logger.info(
            f"Running category-classifier evaluation on {len(test_cases)} test cases..."
        )

        all_metrics: List[CategoryClassifierAgentMetrics] = []
        for test_case in test_cases:
            try:
                metrics = await self.evaluate_single(test_case)
                if metrics is None:
                    continue
                all_metrics.append(metrics)
                status = "✓" if metrics.passed else "✗"
                logger.info(
                    f"{status} {metrics.test_case_id}: "
                    f"primary={metrics.predicted_primary}, "
                    f"secondary={metrics.predicted_secondary}, "
                    f"expected={metrics.expected_category}"
                )
            except Exception as e:
                logger.error(f"Error evaluating {test_case.get('id')}: {e}")
                continue

        return self._summarize(all_metrics)

    def _build_article(self, source: Dict) -> RSSArticle:
        return self.build_rss_article(source)

    def _summarize(
        self, all_metrics: List[CategoryClassifierAgentMetrics]
    ) -> AgentEvaluationResult:
        total = len(all_metrics)
        passed = sum(1 for m in all_metrics if m.passed)
        pass_rate = passed / total if total > 0 else 0.0

        check_names: set[str] = set()
        for m in all_metrics:
            check_names.update(m.checks.keys())
        check_pass_rates = {}
        for check in check_names:
            ctotal = sum(1 for m in all_metrics if check in m.checks)
            cpassed = sum(1 for m in all_metrics if m.checks.get(check, False))
            check_pass_rates[check] = cpassed / ctotal if ctotal > 0 else 0.0

        all_issues: List[str] = []
        for m in all_metrics:
            all_issues.extend(m.issues)
        common_issues = [
            f"{issue} (x{count})" for issue, count in Counter(all_issues).most_common(5)
        ]

        result = AgentEvaluationResult(
            agent_name="category_classifier",
            total_tests=total,
            passed_tests=passed,
            failed_tests=total - passed,
            pass_rate=pass_rate,
            check_pass_rates=check_pass_rates,
            common_issues=common_issues,
            model_used=f"{self.agent.provider}:{self.agent.model}",
        )

        self._save_history(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": result.model_used,
                "total_tests": total,
                "passed_tests": passed,
                "pass_rate": pass_rate,
                "primary_exact_rate": check_pass_rates.get("primary_exact", 0.0),
                "expected_in_predicted_rate": check_pass_rates.get(
                    "expected_in_predicted", 0.0
                ),
                "full_coverage_rate": check_pass_rates.get("full_coverage", 0.0),
                "failed_case_ids": [
                    m.test_case_id for m in all_metrics if not m.passed
                ],
                "common_issues": common_issues,
            }
        )
        self._save_failed_cases(
            "category_classifier",
            [
                {
                    "id": m.test_case_id,
                    "title": m.article_title,
                    "expected_category": m.expected_category,
                    "expected_categories": m.expected_categories,
                    "predicted_primary": m.predicted_primary,
                    "predicted_secondary": m.predicted_secondary,
                    "reasoning": m.reasoning,
                    "issues": m.issues,
                }
                for m in all_metrics
                if not m.passed
            ],
        )

        logger.info(
            f"\nCategory-classifier evaluation: {passed}/{total} passed ({pass_rate:.1%})"
        )
        return result
