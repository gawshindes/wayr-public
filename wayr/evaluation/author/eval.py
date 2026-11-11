"""Author agent evaluation for WAYR platform."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from wayr.agents.author import AuthorAgent
from wayr.evaluation.base_eval import BANNED_ADJECTIVES, BaseEvaluator
from wayr.evaluation.metrics import AgentEvaluationResult, AuthorMetrics
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"


class AuthorEvaluator(BaseEvaluator):
    """Evaluator for author agent quality."""

    def __init__(self, author_agent: AuthorAgent):
        super().__init__(
            agent_name="author",
            history_file=str(_HERE / "history.json"),
        )
        self.author_agent = author_agent

    async def evaluate_single(self, test_case: Dict) -> AuthorMetrics:
        """Evaluate article generation for a single test case."""
        test_id = test_case.get("id", "unknown")
        source = test_case["source"]
        expected = test_case.get("expected", {})

        # Build ClassifiedArticle from test case
        classified = self.build_classified_article(source)

        # Generate article
        generated = await self.author_agent._generate_single(classified)

        content = generated.generated_content
        content_text = self.strip_html(content)

        # Run checks
        word_count = self.count_words(content)
        h3_count = self.count_h3_tags(content)
        banned_found = self.find_banned_adjectives(
            content_text, expected.get("ban_adjectives", BANNED_ADJECTIVES)
        )
        source_url = source.get("url", "")
        has_citation = self.has_source_citation(content, source_url)

        metrics = AuthorMetrics(
            test_case_id=test_id,
            article_title=generated.generated_title,
            word_count=word_count,
            subheading_count=h3_count,
            has_source_citation=has_citation,
            banned_adjectives_found=banned_found,
        )

        # Word count and subheading checks are opt-in per test case — the crisp
        # author relies on chain-of-thought to self-regulate length, so strict
        # minimums aren't enforced by default.
        min_words = expected.get("min_word_count")
        if min_words is not None:
            metrics.checks["word_count"] = word_count >= min_words
            if not metrics.checks["word_count"]:
                metrics.issues.append(
                    f"Word count {word_count} below minimum {min_words}"
                )

        if expected.get("require_subheadings"):
            metrics.checks["has_subheadings"] = 2 <= h3_count <= 5
            if not metrics.checks["has_subheadings"]:
                metrics.issues.append(
                    f"Expected 2-5 <h3> subheadings, found {h3_count}"
                )

        require_citation = expected.get("require_source_citation", True)
        metrics.checks["source_citation"] = (not require_citation) or has_citation
        if not metrics.checks["source_citation"]:
            metrics.issues.append("Source citation missing")

        metrics.checks["no_banned_adjectives"] = len(banned_found) == 0
        if banned_found:
            metrics.issues.append(f"Banned adjectives found: {banned_found}")

        metrics.passed = all(metrics.checks.values())
        return metrics

    async def run_test_suite(self, test_cases_file: str) -> AgentEvaluationResult:
        """Run author evaluation on all test cases."""
        test_cases = self._load_test_cases(test_cases_file)
        logger.info(f"Running author evaluation on {len(test_cases)} test cases...")

        all_metrics: List[AuthorMetrics] = []
        for test_case in test_cases:
            try:
                metrics = await self.evaluate_single(test_case)
                all_metrics.append(metrics)
                status = "\u2713" if metrics.passed else "\u2717"
                logger.info(
                    f"{status} {test_case.get('id', '?')}: "
                    f"{metrics.word_count} words, {metrics.subheading_count} h3s"
                )
            except Exception as e:
                logger.error(f"Error evaluating {test_case.get('id')}: {e}")
                continue

        total = len(all_metrics)
        passed = sum(1 for m in all_metrics if m.passed)
        pass_rate = passed / total if total > 0 else 0.0

        # Per-check pass rates
        check_names: set[str] = set()
        for m in all_metrics:
            check_names.update(m.checks.keys())
        check_pass_rates = {}
        for check in check_names:
            check_total = sum(1 for m in all_metrics if check in m.checks)
            check_passed = sum(1 for m in all_metrics if m.checks.get(check, False))
            check_pass_rates[check] = (
                check_passed / check_total if check_total > 0 else 0.0
            )

        all_issues = []
        for m in all_metrics:
            all_issues.extend(m.issues)
        issue_counts = Counter(all_issues)
        common_issues = [
            f"{issue} (x{count})" for issue, count in issue_counts.most_common(5)
        ]

        result = AgentEvaluationResult(
            agent_name="author",
            total_tests=total,
            passed_tests=passed,
            failed_tests=total - passed,
            pass_rate=pass_rate,
            check_pass_rates=check_pass_rates,
            common_issues=common_issues,
            model_used=f"{self.author_agent.provider}:{self.author_agent.model}",
        )

        self._save_history(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": result.model_used,
                "total_tests": total,
                "passed_tests": passed,
                "pass_rate": pass_rate,
                "avg_word_count": (
                    sum(m.word_count for m in all_metrics) / total if total > 0 else 0
                ),
                "avg_h3_count": (
                    sum(m.subheading_count for m in all_metrics) / total
                    if total > 0
                    else 0
                ),
                "failed_case_ids": [
                    m.test_case_id for m in all_metrics if not m.passed
                ],
                "common_issues": common_issues,
            }
        )

        self._save_failed_cases(
            "author",
            [
                {
                    "id": m.test_case_id,
                    "title": m.article_title,
                    "word_count": m.word_count,
                    "h3_count": m.subheading_count,
                    "banned_adjectives": m.banned_adjectives_found,
                    "issues": m.issues,
                }
                for m in all_metrics
                if not m.passed
            ],
        )

        logger.info(f"\nAuthor evaluation: {passed}/{total} passed ({pass_rate:.1%})")
        return result
