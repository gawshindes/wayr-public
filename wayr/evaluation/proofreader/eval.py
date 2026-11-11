"""Proofreader agent evaluation for WAYR platform."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from wayr.agents.proofreader import ProofreaderAgent
from wayr.evaluation.base_eval import BaseEvaluator
from wayr.evaluation.metrics import AgentEvaluationResult, ProofreaderMetrics
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"


class ProofreaderEvaluator(BaseEvaluator):
    """Evaluator for proofreader agent quality."""

    def __init__(self, proofreader_agent: ProofreaderAgent):
        super().__init__(
            agent_name="proofreader",
            history_file=str(_HERE / "history.json"),
        )
        self.proofreader_agent = proofreader_agent

    async def evaluate_single(self, test_case: Dict) -> ProofreaderMetrics:
        """Evaluate proofreading for a single test case."""
        test_id = test_case.get("id", "unknown")
        input_data = test_case["input"]
        expected = test_case.get("expected", {})

        # Build GeneratedArticle from test case input
        generated = self.build_generated_article(input_data)

        # Run proofreader
        edited = await self.proofreader_agent._proofread_single(generated)

        output_content = edited.edited_content
        output_text = self.strip_html(output_content)

        # Check banned adjectives removal
        should_remove = expected.get("should_remove_adjectives", [])
        remaining = (
            self.find_banned_adjectives(output_text, should_remove)
            if should_remove
            else []
        )

        # Check source citation
        original_url = input_data.get("original_url", "")
        citation_preserved = self.has_source_citation(output_content, original_url)

        # Check AI disclosure
        disclosure_preserved = self.has_ai_disclosure(output_content)

        # Check structure
        h3_count = self.count_h3_tags(output_content)

        # Check word count not shrunk
        input_word_count = self.count_words(input_data.get("generated_content", ""))
        output_word_count = self.count_words(output_content)

        # Check category correction
        category_should_change = expected.get("category_should_change")
        category_correction_correct = None
        if category_should_change is not None:
            if category_should_change:
                expected_category = expected.get("expected_corrected_category")
                if edited.corrected_categories and expected_category:
                    category_correction_correct = (
                        expected_category in edited.corrected_categories
                    )
                else:
                    category_correction_correct = (
                        edited.corrected_categories is not None
                    )
            else:
                category_correction_correct = edited.corrected_categories is None

        metrics = ProofreaderMetrics(
            test_case_id=test_id,
            article_title=edited.edited_title,
            word_count=output_word_count,
            banned_adjectives_remaining=remaining,
            source_citation_preserved=citation_preserved,
            ai_disclosure_preserved=disclosure_preserved,
            subheading_count=h3_count,
            category_correction_correct=category_correction_correct,
        )

        # Checks
        must_preserve_citation = expected.get("must_preserve_source_citation", True)
        must_preserve_disclosure = expected.get("must_preserve_ai_disclosure", True)
        must_have_subheadings = expected.get("must_have_subheadings", True)

        metrics.checks["banned_adjectives_removed"] = len(remaining) == 0
        metrics.checks["source_citation_preserved"] = (
            not must_preserve_citation
        ) or citation_preserved
        metrics.checks["ai_disclosure_preserved"] = (
            not must_preserve_disclosure
        ) or disclosure_preserved
        metrics.checks["has_subheadings"] = (not must_have_subheadings) or h3_count >= 1
        metrics.checks["word_count_acceptable"] = (
            output_word_count >= input_word_count * 0.7
        )

        if category_correction_correct is not None:
            metrics.checks["category_correction"] = category_correction_correct

        # Issues
        if remaining:
            metrics.issues.append(f"Banned adjectives not removed: {remaining}")
        if must_preserve_citation and not citation_preserved:
            metrics.issues.append("Source citation was removed or missing")
        if must_preserve_disclosure and not disclosure_preserved:
            metrics.issues.append("AI disclosure was removed")
        if must_have_subheadings and h3_count < 1:
            metrics.issues.append("No <h3> subheadings in output")
        if output_word_count < input_word_count * 0.7:
            metrics.issues.append(
                f"Word count shrunk too much: {input_word_count} -> {output_word_count}"
            )
        if category_correction_correct is False:
            metrics.issues.append("Category correction incorrect")

        metrics.passed = all(metrics.checks.values())
        return metrics

    async def run_test_suite(self, test_cases_file: str) -> AgentEvaluationResult:
        """Run proofreader evaluation on all test cases."""
        test_cases = self._load_test_cases(test_cases_file)
        logger.info(
            f"Running proofreader evaluation on {len(test_cases)} test cases..."
        )

        all_metrics: List[ProofreaderMetrics] = []
        for test_case in test_cases:
            try:
                metrics = await self.evaluate_single(test_case)
                all_metrics.append(metrics)
                status = "\u2713" if metrics.passed else "\u2717"
                logger.info(
                    f"{status} {test_case.get('id', '?')}: "
                    f"{metrics.word_count} words, "
                    f"{len(metrics.banned_adjectives_remaining)} banned adj remaining"
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
            agent_name="proofreader",
            total_tests=total,
            passed_tests=passed,
            failed_tests=total - passed,
            pass_rate=pass_rate,
            check_pass_rates=check_pass_rates,
            common_issues=common_issues,
            model_used=f"{self.proofreader_agent.provider}:{self.proofreader_agent.model}",
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
            "proofreader",
            [
                {
                    "id": m.test_case_id,
                    "title": m.article_title,
                    "banned_remaining": m.banned_adjectives_remaining,
                    "citation_preserved": m.source_citation_preserved,
                    "disclosure_preserved": m.ai_disclosure_preserved,
                    "issues": m.issues,
                }
                for m in all_metrics
                if not m.passed
            ],
        )

        logger.info(
            f"\nProofreader evaluation: {passed}/{total} passed ({pass_rate:.1%})"
        )
        return result
