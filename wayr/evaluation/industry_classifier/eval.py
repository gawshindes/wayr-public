"""Industry classifier evaluation.

Evaluates IndustryClassifierAgent in isolation: given an article, does it
correctly decide whether the piece belongs to the configured industry? Test
cases use the `expected.belongs_to_industry` bool; if missing, defaults to True
(consistent with the existing classification_test_cases.json, which is already
scoped to tech articles).
"""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from wayr.agents.industry_classifier import IndustryClassifierAgent
from wayr.evaluation.base_eval import BaseEvaluator
from wayr.evaluation.metrics import (
    AgentEvaluationResult,
    IndustryClassifierAgentMetrics,
)
from wayr.models import RSSArticle
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"


class IndustryClassifierEvaluator(BaseEvaluator):
    """Evaluator for Agent A (industry filter)."""

    def __init__(self, agent: IndustryClassifierAgent):
        super().__init__(
            agent_name="industry_classifier",
            history_file=str(_HERE / "history.json"),
        )
        self.agent = agent

    async def evaluate_single(
        self, test_case: Dict
    ) -> Optional[IndustryClassifierAgentMetrics]:
        test_id = test_case.get("id", "unknown")
        expected = test_case.get("expected", {})

        # Default expectation: True (test cases are all in-industry unless
        # otherwise flagged).
        expected_belongs: bool = expected.get("belongs_to_industry", True)

        article = self._build_article(test_case["source"])
        result = await self.agent.check(article)

        metrics = IndustryClassifierAgentMetrics(
            test_case_id=test_id,
            article_title=article.title,
            predicted_belongs=result.belongs_to_industry,
            expected_belongs=expected_belongs,
            reasoning=result.reasoning,
        )

        correct = result.belongs_to_industry == expected_belongs
        metrics.checks["industry_correct"] = correct
        if not correct:
            metrics.issues.append(
                f"Expected belongs_to_industry={expected_belongs}, "
                f"got {result.belongs_to_industry}"
            )

        metrics.passed = correct
        return metrics

    async def run_test_suite(self, test_cases_file: str) -> AgentEvaluationResult:
        test_cases = self._load_test_cases(test_cases_file)
        logger.info(
            f"Running industry-classifier evaluation on {len(test_cases)} test cases..."
        )

        all_metrics: List[IndustryClassifierAgentMetrics] = []
        for test_case in test_cases:
            try:
                metrics = await self.evaluate_single(test_case)
                if metrics is None:
                    continue
                all_metrics.append(metrics)
                status = "✓" if metrics.passed else "✗"
                logger.info(
                    f"{status} {metrics.test_case_id}: "
                    f"predicted={metrics.predicted_belongs}, "
                    f"expected={metrics.expected_belongs}"
                )
            except Exception as e:
                logger.error(f"Error evaluating {test_case.get('id')}: {e}")
                continue

        return self._summarize(all_metrics)

    def _build_article(self, source: Dict) -> RSSArticle:
        return self.build_rss_article(source)

    def _summarize(
        self, all_metrics: List[IndustryClassifierAgentMetrics]
    ) -> AgentEvaluationResult:
        total = len(all_metrics)
        passed = sum(1 for m in all_metrics if m.passed)
        pass_rate = passed / total if total > 0 else 0.0

        check_pass_rates = _check_pass_rates(all_metrics)
        common_issues = _common_issues(all_metrics)

        result = AgentEvaluationResult(
            agent_name="industry_classifier",
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
                "failed_case_ids": [
                    m.test_case_id for m in all_metrics if not m.passed
                ],
                "common_issues": common_issues,
            }
        )
        self._save_failed_cases(
            "industry_classifier",
            [
                {
                    "id": m.test_case_id,
                    "title": m.article_title,
                    "expected_belongs": m.expected_belongs,
                    "predicted_belongs": m.predicted_belongs,
                    "reasoning": m.reasoning,
                    "issues": m.issues,
                }
                for m in all_metrics
                if not m.passed
            ],
        )

        logger.info(
            f"\nIndustry-classifier evaluation: {passed}/{total} passed ({pass_rate:.1%})"
        )
        return result


def _check_pass_rates(all_metrics):
    if not all_metrics:
        return {}
    check_names: set[str] = set()
    for m in all_metrics:
        check_names.update(m.checks.keys())
    rates = {}
    for check in check_names:
        total = sum(1 for m in all_metrics if check in m.checks)
        passed = sum(1 for m in all_metrics if m.checks.get(check, False))
        rates[check] = passed / total if total > 0 else 0.0
    return rates


def _common_issues(all_metrics):
    all_issues: List[str] = []
    for m in all_metrics:
        all_issues.extend(m.issues)
    return [
        f"{issue} (x{count})" for issue, count in Counter(all_issues).most_common(5)
    ]
