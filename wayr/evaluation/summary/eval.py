"""Summary agent evaluation for WAYR platform."""

import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from wayr.agents.summary import SummaryAgent
from wayr.evaluation.base_eval import BaseEvaluator
from wayr.evaluation.metrics import AgentEvaluationResult, SummaryMetrics
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"


class SummaryEvaluator(BaseEvaluator):
    """Evaluator for summary agent quality."""

    def __init__(self, summary_agent: SummaryAgent):
        super().__init__(
            agent_name="summary",
            history_file=str(_HERE / "history.json"),
        )
        self.summary_agent = summary_agent

    @staticmethod
    def _count_sentences(text: str) -> int:
        """Count sentences by splitting on sentence-ending punctuation."""
        # Split on . ! ? followed by space or end of string
        sentences = re.split(r"[.!?](?:\s|$)", text.strip())
        # Filter out empty strings
        return len([s for s in sentences if s.strip()])

    @staticmethod
    def _title_repeated(title: str, summary: str) -> bool:
        """Check if the title appears verbatim (or nearly) in the summary."""
        title_lower = title.lower().strip()
        summary_lower = summary.lower()
        # Exact match
        if title_lower in summary_lower:
            return True
        # Check if 80%+ of title words appear sequentially
        title_words = title_lower.split()
        if len(title_words) >= 4:
            threshold = int(len(title_words) * 0.8)
            for i in range(len(title_words) - threshold + 1):
                chunk = " ".join(title_words[i : i + threshold])
                if chunk in summary_lower:
                    return True
        return False

    async def evaluate_single(self, test_case: Dict) -> SummaryMetrics:
        """Evaluate summary generation for a single test case."""
        test_id = test_case.get("id", "unknown")
        input_data = test_case["input"]
        expected = test_case.get("expected", {})

        # Build a minimal EditedArticle
        generated = self.build_generated_article(
            {
                "generated_title": input_data["edited_title"],
                "generated_content": input_data["edited_content"],
                "original_title": input_data.get(
                    "original_title", input_data["edited_title"]
                ),
            }
        )
        from wayr.models import EditedArticle

        article = EditedArticle(
            generated=generated,
            edited_title=input_data["edited_title"],
            edited_content=input_data["edited_content"],
            edit_summary="Test case",
            editor_model="test",
        )

        # Generate summary
        summary = await self.summary_agent._generate_summary(article)

        # Run checks
        char_count = len(summary)
        sentence_count = self._count_sentences(summary)
        title_repeated = self._title_repeated(input_data["edited_title"], summary)

        min_chars = expected.get("min_chars", 150)
        max_chars = expected.get("max_chars", 320)
        min_sentences = expected.get("min_sentences", 2)
        max_sentences = expected.get("max_sentences", 3)

        metrics = SummaryMetrics(
            test_case_id=test_id,
            article_title=input_data["edited_title"],
            char_count=char_count,
            sentence_count=sentence_count,
        )

        # Checks
        metrics.checks["char_count_in_range"] = min_chars <= char_count <= max_chars
        metrics.checks["sentence_count_in_range"] = (
            min_sentences <= sentence_count <= max_sentences
        )
        metrics.checks["no_title_repetition"] = not title_repeated

        # Issues
        if not metrics.checks["char_count_in_range"]:
            metrics.issues.append(
                f"Character count {char_count} outside range [{min_chars}, {max_chars}]"
            )
        if not metrics.checks["sentence_count_in_range"]:
            metrics.issues.append(
                f"Sentence count {sentence_count} outside range [{min_sentences}, {max_sentences}]"
            )
        if title_repeated:
            metrics.issues.append("Title repeated in summary")

        # Key facts check (soft — reported but not gating)
        key_facts = expected.get("key_facts_should_mention", [])
        if key_facts:
            summary_lower = summary.lower()
            found_facts = [f for f in key_facts if f.lower() in summary_lower]
            metrics.checks["key_facts_mentioned"] = len(found_facts) > 0
            if not metrics.checks["key_facts_mentioned"]:
                metrics.issues.append(
                    f"No key facts mentioned (expected any of: {key_facts})"
                )

        # Pass = all hard checks pass
        metrics.passed = (
            metrics.checks["char_count_in_range"]
            and metrics.checks["sentence_count_in_range"]
            and metrics.checks["no_title_repetition"]
        )

        return metrics

    async def run_test_suite(self, test_cases_file: str) -> AgentEvaluationResult:
        """Run summary evaluation on all test cases."""
        test_cases = self._load_test_cases(test_cases_file)
        logger.info(f"Running summary evaluation on {len(test_cases)} test cases...")

        all_metrics: List[SummaryMetrics] = []
        for test_case in test_cases:
            try:
                metrics = await self.evaluate_single(test_case)
                all_metrics.append(metrics)
                status = "\u2713" if metrics.passed else "\u2717"
                logger.info(
                    f"{status} {test_case.get('id', '?')}: "
                    f"{metrics.char_count} chars, {metrics.sentence_count} sentences"
                )
            except Exception as e:
                logger.error(f"Error evaluating {test_case.get('id')}: {e}")
                continue

        # Aggregate
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

        # Common issues
        all_issues = []
        for m in all_metrics:
            all_issues.extend(m.issues)
        issue_counts = Counter(all_issues)
        common_issues = [
            f"{issue} (x{count})" for issue, count in issue_counts.most_common(5)
        ]

        result = AgentEvaluationResult(
            agent_name="summary",
            total_tests=total,
            passed_tests=passed,
            failed_tests=total - passed,
            pass_rate=pass_rate,
            check_pass_rates=check_pass_rates,
            common_issues=common_issues,
            model_used=f"{self.summary_agent.provider}:{self.summary_agent.model}",
        )

        # Save history
        self._save_history(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": result.model_used,
                "total_tests": total,
                "passed_tests": passed,
                "pass_rate": pass_rate,
                "avg_char_count": (
                    sum(m.char_count for m in all_metrics) / total if total > 0 else 0
                ),
                "avg_sentence_count": (
                    sum(m.sentence_count for m in all_metrics) / total
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
            "summary",
            [
                {
                    "id": m.test_case_id,
                    "title": m.article_title,
                    "char_count": m.char_count,
                    "sentence_count": m.sentence_count,
                    "issues": m.issues,
                }
                for m in all_metrics
                if not m.passed
            ],
        )

        logger.info(f"\nSummary evaluation: {passed}/{total} passed ({pass_rate:.1%})")
        return result
