"""News classifier (Agent B) evaluation.

Evaluates NewsClassifierAgent in isolation on the content_type decision
(NEWS vs DEAL/OPINION/REPORT/REVIEW/OTHER). Test cases require
`expected.content_type`; legacy 'guide' values are remapped to 'other' to match
the current enum.

Cases where `expected.belongs_to_industry` is False are skipped — Agent B
shouldn't be judged on articles Agent A is meant to reject upstream.
"""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from wayr.agents.news_classifier import NewsClassifierAgent
from wayr.evaluation.base_eval import BaseEvaluator
from wayr.evaluation.metrics import (
    AgentEvaluationResult,
    NewsClassifierAgentMetrics,
)
from wayr.models import RSSArticle
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"

_LEGACY_CONTENT_TYPE_MAP = {"guide": "other"}


class NewsClassifierEvaluator(BaseEvaluator):
    """Evaluator for Agent B (news vs non-news + subcategory)."""

    def __init__(self, agent: NewsClassifierAgent):
        super().__init__(
            agent_name="news_classifier",
            history_file=str(_HERE / "history.json"),
        )
        self.agent = agent

    async def evaluate_single(
        self, test_case: Dict
    ) -> Optional[NewsClassifierAgentMetrics]:
        test_id = test_case.get("id", "unknown")
        expected = test_case.get("expected", {})

        # Skip cases flagged as out-of-industry.
        if expected.get("belongs_to_industry") is False:
            return None

        expected_content_type = expected.get("content_type")
        if not expected_content_type:
            return None
        expected_content_type = _LEGACY_CONTENT_TYPE_MAP.get(
            expected_content_type, expected_content_type
        )

        article = self._build_article(test_case["source"])
        result = await self.agent.classify(article)
        predicted = result.content_type.value

        metrics = NewsClassifierAgentMetrics(
            test_case_id=test_id,
            article_title=article.title,
            predicted_content_type=predicted,
            expected_content_type=expected_content_type,
            reasoning=result.reasoning,
            source_origin=article.source_origin,
        )

        # Exact subcategory match
        exact_correct = predicted == expected_content_type
        metrics.checks["content_type_correct"] = exact_correct

        # Coarser NEWS-vs-NON-NEWS correctness (often the more critical signal)
        predicted_is_news = predicted == "news"
        expected_is_news = expected_content_type == "news"
        news_correct = predicted_is_news == expected_is_news
        metrics.news_vs_nonnews_correct = news_correct
        metrics.checks["news_vs_nonnews_correct"] = news_correct

        if not exact_correct:
            metrics.issues.append(
                f"Expected content_type='{expected_content_type}', got '{predicted}'"
            )
        if not news_correct:
            metrics.issues.append(
                f"NEWS vs NON-NEWS mismatch: expected_is_news={expected_is_news}, "
                f"predicted_is_news={predicted_is_news} (CRITICAL)"
            )

        # Overall pass = NEWS-vs-NON-NEWS decision is correct. Sub-type
        # misclassifications are tracked separately but don't fail the case,
        # since only the NEWS bit drives downstream pipeline behavior.
        metrics.passed = news_correct
        return metrics

    async def run_test_suite(self, test_cases_file: str) -> AgentEvaluationResult:
        test_cases = self._load_test_cases(test_cases_file)
        logger.info(
            f"Running news-classifier evaluation on {len(test_cases)} test cases..."
        )

        all_metrics: List[NewsClassifierAgentMetrics] = []
        for test_case in test_cases:
            try:
                metrics = await self.evaluate_single(test_case)
                if metrics is None:
                    continue
                all_metrics.append(metrics)
                status = "✓" if metrics.passed else "✗"
                logger.info(
                    f"{status} {metrics.test_case_id}: "
                    f"predicted={metrics.predicted_content_type}, "
                    f"expected={metrics.expected_content_type}"
                )
            except Exception as e:
                logger.error(f"Error evaluating {test_case.get('id')}: {e}")
                continue

        return self._summarize(all_metrics)

    def _build_article(self, source: Dict) -> RSSArticle:
        return self.build_rss_article(source)

    def _summarize(
        self, all_metrics: List[NewsClassifierAgentMetrics]
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

        # Pass rate per source origin, when any case declares one: the brand
        # path gets its own input (title + first-party hint), so it must be
        # measured on its own too. Cases without an origin count as press.
        pass_rate_by_origin: Dict[str, float] = {}
        if any(m.source_origin for m in all_metrics):
            for origin in ("press", "brand"):
                group = [
                    m for m in all_metrics if (m.source_origin or "press") == origin
                ]
                if group:
                    rate = sum(1 for m in group if m.passed) / len(group)
                    pass_rate_by_origin[origin] = rate
                    check_pass_rates[f"origin={origin}"] = rate
                    logger.info(
                        f"  origin={origin}: {sum(1 for m in group if m.passed)}/{len(group)} passed"
                    )

        all_issues: List[str] = []
        for m in all_metrics:
            all_issues.extend(m.issues)
        common_issues = [
            f"{issue} (x{count})" for issue, count in Counter(all_issues).most_common(5)
        ]

        result = AgentEvaluationResult(
            agent_name="news_classifier",
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
                "content_type_exact_rate": check_pass_rates.get(
                    "content_type_correct", 0.0
                ),
                "news_vs_nonnews_rate": check_pass_rates.get(
                    "news_vs_nonnews_correct", 0.0
                ),
                "pass_rate_by_origin": pass_rate_by_origin,
                "failed_case_ids": [
                    m.test_case_id for m in all_metrics if not m.passed
                ],
                "common_issues": common_issues,
            }
        )
        self._save_failed_cases(
            "news_classifier",
            [
                {
                    "id": m.test_case_id,
                    "title": m.article_title,
                    "expected_content_type": m.expected_content_type,
                    "predicted_content_type": m.predicted_content_type,
                    "reasoning": m.reasoning,
                    "issues": m.issues,
                }
                for m in all_metrics
                if not m.passed
            ],
        )

        logger.info(
            f"\nNews-classifier evaluation: {passed}/{total} passed ({pass_rate:.1%})"
        )
        return result
