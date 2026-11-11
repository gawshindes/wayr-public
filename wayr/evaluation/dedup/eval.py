"""Deduplication agent evaluation for WAYR platform."""

from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Dict, FrozenSet, List, Set

from wayr.agents.deduplication import DeduplicationAgent
from wayr.evaluation.base_eval import BaseEvaluator
from wayr.evaluation.metrics import AgentEvaluationResult, DeduplicationMetrics
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"


class DeduplicationEvaluator(BaseEvaluator):
    """Evaluator for deduplication agent quality."""

    def __init__(self, dedup_agent: DeduplicationAgent):
        super().__init__(
            agent_name="deduplication",
            history_file=str(_HERE / "history.json"),
        )
        self.dedup_agent = dedup_agent

    @staticmethod
    def _groups_to_pairs(groups: List[List[int]]) -> Set[FrozenSet[int]]:
        """Convert groups to sets of pairs for comparison."""
        pairs = set()
        for group in groups:
            for a, b in combinations(group, 2):
                pairs.add(frozenset({a, b}))
        return pairs

    async def evaluate_single(self, test_case: Dict) -> DeduplicationMetrics:
        """Evaluate dedup on a single test batch."""
        test_id = test_case.get("id", "unknown")
        input_data = test_case["input"]
        expected = test_case.get("expected", {})

        batch_titles = input_data["batch_titles"]
        cached_titles = input_data.get("cached_titles", [])

        # Run dedup agent
        result = await self.dedup_agent.find_duplicates(
            batch_titles=batch_titles,
            cached_titles=cached_titles if cached_titles else None,
        )

        predicted_groups = result.get("duplicate_groups", [])
        expected_groups = expected.get("duplicate_groups", [])
        must_not_group = expected.get("must_not_group", [])

        # Convert to pairs for comparison
        expected_pairs = self._groups_to_pairs(expected_groups)
        predicted_pairs = self._groups_to_pairs(predicted_groups)
        must_not_pairs = {frozenset(pair) for pair in must_not_group}

        # Calculate TP/FP/FN
        tp = len(expected_pairs & predicted_pairs)
        fp = len(predicted_pairs - expected_pairs)
        fn = len(expected_pairs - predicted_pairs)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0

        # Check must_not_group constraints
        violated_pairs = predicted_pairs & must_not_pairs

        # Validate indices
        all_input_indices = {t["index"] for t in batch_titles}
        if cached_titles:
            all_input_indices |= {t["index"] for t in cached_titles}
        all_predicted_indices = {idx for group in predicted_groups for idx in group}
        invalid_indices = all_predicted_indices - all_input_indices

        # Build metrics
        metrics = DeduplicationMetrics(
            test_case_id=test_id,
            batch_name=test_case.get("description", test_id),
            expected_groups=expected_groups,
            predicted_groups=predicted_groups,
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            precision=precision,
            recall=recall,
        )

        # Checks
        metrics.checks["no_false_positives"] = fp == 0
        metrics.checks["must_not_group_respected"] = len(violated_pairs) == 0
        metrics.checks["valid_indices"] = len(invalid_indices) == 0
        metrics.checks["recall_acceptable"] = recall >= 0.70 or len(expected_pairs) == 0

        # Issues
        if fp > 0:
            metrics.issues.append(f"{fp} false positive pair(s) detected")
        if violated_pairs:
            metrics.issues.append(
                f"must_not_group violated: {[list(p) for p in violated_pairs]}"
            )
        if invalid_indices:
            metrics.issues.append(f"Invalid indices in output: {invalid_indices}")
        if fn > 0:
            metrics.issues.append(f"{fn} duplicate pair(s) missed (false negatives)")

        # Pass if no FPs and no must_not_group violations
        metrics.passed = (
            metrics.checks["no_false_positives"]
            and metrics.checks["must_not_group_respected"]
            and metrics.checks["valid_indices"]
        )

        return metrics

    async def run_test_suite(self, test_cases_file: str) -> AgentEvaluationResult:
        """Run dedup evaluation on all test cases."""
        test_cases = self._load_test_cases(test_cases_file)
        logger.info(
            f"Running deduplication evaluation on {len(test_cases)} test cases..."
        )

        all_metrics: List[DeduplicationMetrics] = []
        for test_case in test_cases:
            try:
                metrics = await self.evaluate_single(test_case)
                all_metrics.append(metrics)
                status = "\u2713" if metrics.passed else "\u2717"
                logger.info(
                    f"{status} {test_case.get('id', '?')}: "
                    f"P={metrics.precision:.2f} R={metrics.recall:.2f} "
                    f"FP={metrics.false_positives}"
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
            agent_name="deduplication",
            total_tests=total,
            passed_tests=passed,
            failed_tests=total - passed,
            pass_rate=pass_rate,
            check_pass_rates=check_pass_rates,
            common_issues=common_issues,
            model_used=f"{self.dedup_agent.provider}:{self.dedup_agent.model}",
        )

        # Save history
        self._save_history(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "model": result.model_used,
                "total_tests": total,
                "passed_tests": passed,
                "pass_rate": pass_rate,
                "avg_precision": (
                    sum(m.precision for m in all_metrics) / total if total > 0 else 0
                ),
                "avg_recall": (
                    sum(m.recall for m in all_metrics) / total if total > 0 else 0
                ),
                "total_false_positives": sum(m.false_positives for m in all_metrics),
                "failed_case_ids": [
                    m.test_case_id for m in all_metrics if not m.passed
                ],
                "common_issues": common_issues,
            }
        )

        # Save failed cases
        self._save_failed_cases(
            "dedup",
            [
                {
                    "id": m.test_case_id,
                    "expected_groups": m.expected_groups,
                    "predicted_groups": m.predicted_groups,
                    "issues": m.issues,
                }
                for m in all_metrics
                if not m.passed
            ],
        )

        logger.info(f"\nDedup evaluation: {passed}/{total} passed ({pass_rate:.1%})")
        return result
