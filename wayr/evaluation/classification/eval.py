"""Classification evaluation for WAYR platform.

Evaluates the three classification sub-agents (A: industry filter, B: news vs
non-news, C: categories) composed in the same A→B→C order used in production.
Reports aggregate pass rates plus per-subagent pass rates so regressions can
be attributed to the right stage.
"""

import json
from pathlib import Path
from typing import List, Dict, Optional
from collections import Counter

from wayr.agents.industry_classifier import IndustryClassifierAgent
from wayr.agents.news_classifier import NewsClassifierAgent
from wayr.agents.category_classifier import CategoryClassifierAgent
from wayr.models import RSSArticle
from wayr.models.articles import ClassifiedArticle, ContentType
from wayr.evaluation.metrics import (
    ClassificationMetrics,
    EvaluationResult,
    CategoryMetrics,
    ConfusionMatrix,
)
from wayr.utils import get_logger

logger = get_logger(__name__)

_HERE = Path(__file__).parent
DEFAULT_TEST_CASES = _HERE / "test_cases.json"


class ClassificationEvaluator:
    """Evaluator for the three-agent classification pipeline."""

    def __init__(
        self,
        industry_classifier_agent: IndustryClassifierAgent,
        news_classifier_agent: NewsClassifierAgent,
        category_classifier_agent: CategoryClassifierAgent,
        confidence_threshold: float = 0.6,
    ):
        self.industry_classifier_agent = industry_classifier_agent
        self.news_classifier_agent = news_classifier_agent
        self.category_classifier_agent = category_classifier_agent
        self.confidence_threshold = confidence_threshold

    async def _classify(self, article: RSSArticle) -> ClassifiedArticle:
        """Run industry → news → category with early-exit, mirroring the orchestrator."""
        a = await self.industry_classifier_agent.check(article)
        if not a.belongs_to_industry:
            return ClassifiedArticle(
                original=article,
                belongs_to_industry=False,
                industry_reasoning=a.reasoning,
                content_type=ContentType.OTHER,
                content_type_confidence=1.0,
                content_type_reason="Rejected by industry classifier",
                categories=[],
                confidence=0.0,
                classification_reason="Not part of target industry",
            )
        b = await self.news_classifier_agent.classify(article)
        if b.content_type != ContentType.NEWS:
            return ClassifiedArticle(
                original=article,
                belongs_to_industry=True,
                industry_reasoning=a.reasoning,
                content_type=b.content_type,
                content_type_confidence=1.0,
                content_type_reason=b.reasoning,
                categories=[],
                confidence=0.0,
                classification_reason=f"Non-news ({b.content_type.value})",
            )
        c = await self.category_classifier_agent.categorize(article)
        return ClassifiedArticle(
            original=article,
            belongs_to_industry=True,
            industry_reasoning=a.reasoning,
            content_type=ContentType.NEWS,
            content_type_confidence=1.0,
            content_type_reason=b.reasoning,
            categories=[c.primary, c.secondary],
            category_confidences=[1.0, 0.8],
            confidence=1.0,
            classification_reason=c.reasoning,
        )

    async def evaluate_single(
        self, article: RSSArticle, expected: Optional[Dict] = None
    ) -> ClassificationMetrics:
        classified = await self._classify(article)

        predicted_categories = list(classified.categories)
        metrics = ClassificationMetrics(
            article_title=article.title,
            article_url=str(article.url),
            predicted_content_type=classified.content_type.value,
            predicted_category=classified.category or "",
            predicted_categories=predicted_categories,
            predicted_confidence=classified.content_type_confidence,
            classification_reason=classified.content_type_reason,
            confidence_acceptable=classified.content_type_confidence
            >= self.confidence_threshold,
        )

        if not expected:
            return metrics

        metrics.expected_content_type = expected.get("content_type")
        metrics.expected_category = expected.get("category")
        metrics.expected_categories = expected.get("categories")
        expected_belongs = expected.get("belongs_to_industry")

        # Agent A — industry filter correctness
        if expected_belongs is not None:
            metrics.industry_correct = classified.belongs_to_industry == bool(
                expected_belongs
            )

        # Agent B — content type correctness (only meaningful when A passed)
        if metrics.expected_content_type and classified.belongs_to_industry:
            metrics.content_type_correct = (
                metrics.predicted_content_type == metrics.expected_content_type
            )

        # Agent C — category correctness (only meaningful when B == news)
        if metrics.expected_category and metrics.predicted_content_type == "news":
            metrics.category_correct = metrics.expected_category in predicted_categories

        if metrics.expected_categories and metrics.predicted_content_type == "news":
            expected_set = set(metrics.expected_categories)
            predicted_set = set(predicted_categories)
            metrics.categories_coverage_correct = expected_set.issubset(predicted_set)

        expected_confidence_min = expected.get(
            "content_type_confidence_min", self.confidence_threshold
        )
        if metrics.predicted_confidence < expected_confidence_min:
            metrics.confidence_acceptable = False
            metrics.issues.append(
                f"Low confidence: {metrics.predicted_confidence:.2f} < {expected_confidence_min:.2f}"
            )

        metrics.passed = True

        if metrics.industry_correct is False:
            metrics.passed = False
            metrics.issues.append(
                f"Wrong industry filter: expected belongs_to_industry={expected_belongs}, "
                f"got {classified.belongs_to_industry}"
            )

        if metrics.content_type_correct is False:
            metrics.passed = False
            metrics.issues.append(
                f"Wrong content type: expected {metrics.expected_content_type}, "
                f"got {metrics.predicted_content_type}"
            )

        if metrics.category_correct is False:
            metrics.passed = False
            metrics.issues.append(
                f"Wrong category: expected {metrics.expected_category}, "
                f"got {metrics.predicted_category} (all: {predicted_categories})"
            )

        if not metrics.confidence_acceptable:
            metrics.passed = False

        return metrics

    async def run_test_suite(self, test_cases_file: str) -> EvaluationResult:
        test_cases = self._load_test_cases(test_cases_file)

        logger.info(
            f"Running classification evaluation on {len(test_cases)} test cases..."
        )

        all_metrics: List[ClassificationMetrics] = []
        for test_case in test_cases:
            try:
                article = self._create_article_from_test_case(test_case)
                metrics = await self.evaluate_single(
                    article=article, expected=test_case.get("expected")
                )
                all_metrics.append(metrics)
                status = "✓" if metrics.passed else "✗"
                logger.info(
                    f"{status} {test_case.get('id', 'unknown')}: "
                    f"{metrics.predicted_content_type} "
                    f"(confidence: {metrics.predicted_confidence:.2f})"
                )
            except Exception as e:
                logger.error(f"Error evaluating test case {test_case.get('id')}: {e}")
                continue

        total = len(all_metrics)
        passed = sum(1 for m in all_metrics if m.passed)
        failed = total - passed
        pass_rate = passed / total if total > 0 else 0.0

        # Agent A — industry filter pass rate
        industry_total = sum(1 for m in all_metrics if m.industry_correct is not None)
        industry_passed = sum(1 for m in all_metrics if m.industry_correct is True)
        industry_pass_rate = (
            industry_passed / industry_total if industry_total > 0 else 0.0
        )

        # Agent B — news-vs-non-news pass rate (L1)
        l1_total = sum(1 for m in all_metrics if m.content_type_correct is not None)
        l1_passed = sum(1 for m in all_metrics if m.content_type_correct is True)
        l1_failed = sum(1 for m in all_metrics if m.content_type_correct is False)
        l1_pass_rate = l1_passed / l1_total if l1_total > 0 else 0.0

        # Agent C — category pass rate (L2)
        l2_total = sum(1 for m in all_metrics if m.category_correct is not None)
        l2_passed = sum(1 for m in all_metrics if m.category_correct is True)
        l2_failed = sum(1 for m in all_metrics if m.category_correct is False)
        l2_pass_rate = l2_passed / l2_total if l2_total > 0 else 0.0

        common_issues = self._analyze_issues(all_metrics)
        recommendations = self._generate_recommendations(all_metrics)
        category_metrics = self._calculate_detailed_metrics(all_metrics)
        confusion_matrix = self._generate_confusion_matrix(all_metrics)

        self._save_failed_cases(all_metrics)

        result = EvaluationResult(
            total_tests=total,
            passed_tests=passed,
            failed_tests=failed,
            pass_rate=pass_rate,
            industry_pass_rate=industry_pass_rate,
            industry_passed=industry_passed,
            industry_failed=industry_total - industry_passed,
            l1_pass_rate=l1_pass_rate,
            l2_pass_rate=l2_pass_rate,
            l1_passed=l1_passed,
            l1_failed=l1_failed,
            l2_passed=l2_passed,
            l2_failed=l2_failed,
            metrics=all_metrics,
            category_metrics=category_metrics,
            confusion_matrix=confusion_matrix,
            common_issues=common_issues,
            recommendations=recommendations,
        )

        self._save_evaluation_history(result, all_metrics)

        logger.info(
            f"\nEvaluation complete: {passed}/{total} passed ({pass_rate:.1%}) | "
            f"A: {industry_pass_rate:.1%}, B: {l1_pass_rate:.1%}, C: {l2_pass_rate:.1%}"
        )

        return result

    def _load_test_cases(self, test_cases_file: str) -> List[Dict]:
        file_path = Path(test_cases_file)
        if not file_path.exists():
            raise FileNotFoundError(f"Test cases file not found: {test_cases_file}")

        with open(file_path, "r") as f:
            data = json.load(f)

        return data.get("test_cases", [])

    def _create_article_from_test_case(self, test_case: Dict) -> RSSArticle:
        from datetime import datetime, timezone
        from pydantic import HttpUrl

        source = test_case["source"]

        return RSSArticle(
            title=source["title"],
            url=HttpUrl(source["url"]),
            source=source.get("source", "Test Source"),
            published_date=datetime.now(timezone.utc),
            summary=source.get("summary"),
            content=source.get("content"),
            tags=source.get("tags", []),
            source_origin=source.get("source_origin"),
        )

    def _analyze_issues(self, metrics: List[ClassificationMetrics]) -> List[str]:
        issues = []

        industry_errors = sum(1 for m in metrics if m.industry_correct is False)
        content_type_errors = sum(1 for m in metrics if m.content_type_correct is False)
        category_errors = sum(1 for m in metrics if m.category_correct is False)
        confidence_errors = sum(1 for m in metrics if not m.confidence_acceptable)

        if industry_errors > 0:
            issues.append(f"{industry_errors} industry filter misclassifications")

        if content_type_errors > 0:
            issues.append(f"{content_type_errors} content type misclassifications")

        if category_errors > 0:
            issues.append(f"{category_errors} category misclassifications")

        if confidence_errors > 0:
            issues.append(f"{confidence_errors} low confidence classifications")

        opinion_as_news = sum(
            1
            for m in metrics
            if m.expected_content_type == "opinion"
            and m.predicted_content_type == "news"
        )
        if opinion_as_news > 0:
            issues.append(
                f"{opinion_as_news} opinion articles classified as news (CRITICAL)"
            )

        return issues

    def _generate_recommendations(
        self, metrics: List[ClassificationMetrics]
    ) -> List[str]:
        recommendations = []

        opinion_as_news = [
            m
            for m in metrics
            if m.expected_content_type == "opinion"
            and m.predicted_content_type == "news"
        ]
        if opinion_as_news:
            recommendations.append(
                "CRITICAL: Strengthen opinion detection in news classifier prompt."
            )

        low_confidence = [m for m in metrics if not m.confidence_acceptable]
        if len(low_confidence) > len(metrics) * 0.3:
            recommendations.append(
                "High percentage of low confidence classifications. "
                "Consider improving prompt clarity or using a more capable model."
            )

        category_errors = [m for m in metrics if m.category_correct is False]
        if category_errors:
            confused = [
                (m.expected_category, m.predicted_category) for m in category_errors
            ]
            most_common_confusion = Counter(confused).most_common(1)
            if most_common_confusion:
                expected, predicted = most_common_confusion[0][0]
                recommendations.append(
                    f"Category confusion detected: {expected} often misclassified as {predicted}. "
                    "Tune category classifier prompt or allowed category list."
                )

        return recommendations

    def _calculate_detailed_metrics(
        self, metrics: List[ClassificationMetrics]
    ) -> Dict[str, CategoryMetrics]:
        categories = set()
        for m in metrics:
            if m.expected_category:
                categories.add(m.expected_category)
            if m.predicted_category:
                categories.add(m.predicted_category)
        categories.discard("")

        results = {}
        for cat in categories:
            tp = sum(
                1
                for m in metrics
                if m.expected_category == cat and m.predicted_category == cat
            )
            fp = sum(
                1
                for m in metrics
                if m.expected_category != cat and m.predicted_category == cat
            )
            fn = sum(
                1
                for m in metrics
                if m.expected_category == cat and m.predicted_category != cat
            )
            support = sum(1 for m in metrics if m.expected_category == cat)

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = (
                2 * (precision * recall) / (precision + recall)
                if (precision + recall) > 0
                else 0.0
            )

            results[cat] = CategoryMetrics(
                precision=precision, recall=recall, f1_score=f1, support=support
            )

        return results

    def _generate_confusion_matrix(
        self, metrics: List[ClassificationMetrics]
    ) -> ConfusionMatrix:
        categories = set()
        for m in metrics:
            if m.expected_category:
                categories.add(m.expected_category)
            if m.predicted_category:
                categories.add(m.predicted_category)

        labels = sorted([c for c in categories if c])
        size = len(labels)
        matrix = [[0 for _ in range(size)] for _ in range(size)]
        label_to_idx = {label: i for i, label in enumerate(labels)}

        for m in metrics:
            expected = m.expected_category
            predicted = m.predicted_category
            if expected in label_to_idx and predicted in label_to_idx:
                i = label_to_idx[expected]
                j = label_to_idx[predicted]
                matrix[i][j] += 1

        return ConfusionMatrix(labels=labels, matrix=matrix)

    def _save_failed_cases(self, metrics: List[ClassificationMetrics]):
        failed_cases = []
        for m in metrics:
            if not m.passed:
                failed_cases.append(
                    {
                        "title": m.article_title,
                        "url": m.article_url,
                        "expected": {
                            "content_type": m.expected_content_type,
                            "category": m.expected_category,
                        },
                        "predicted": {
                            "content_type": m.predicted_content_type,
                            "category": m.predicted_category,
                            "confidence": m.predicted_confidence,
                            "reason": m.classification_reason,
                        },
                        "issues": m.issues,
                    }
                )

        if failed_cases:
            output_file = _HERE / "failed_classification_cases.json"
            with open(output_file, "w") as f:
                json.dump(failed_cases, f, indent=2)
            logger.info(f"Saved {len(failed_cases)} failed cases to {output_file}")

    def _save_evaluation_history(
        self, result: EvaluationResult, metrics: List[ClassificationMetrics]
    ):
        from datetime import datetime, timezone

        history_file = _HERE / "history.json"

        history = []
        if history_file.exists():
            with open(history_file, "r") as f:
                history = json.load(f)

        failed_test_ids = [m.article_title for m in metrics if not m.passed]

        run_summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "models": {
                "industry": f"{self.industry_classifier_agent.provider}:{self.industry_classifier_agent.model}",
                "news": f"{self.news_classifier_agent.provider}:{self.news_classifier_agent.model}",
                "category": f"{self.category_classifier_agent.provider}:{self.category_classifier_agent.model}",
            },
            "total_tests": result.total_tests,
            "passed_tests": result.passed_tests,
            "failed_tests": result.failed_tests,
            "pass_rate": result.pass_rate,
            "industry_pass_rate": result.industry_pass_rate,
            "l1_pass_rate": result.l1_pass_rate,
            "l1_passed": result.l1_passed,
            "l1_failed": result.l1_failed,
            "l2_pass_rate": result.l2_pass_rate,
            "l2_passed": result.l2_passed,
            "l2_failed": result.l2_failed,
            "failed_test_titles": failed_test_ids,
            "common_issues": result.common_issues,
        }

        history.append(run_summary)
        if len(history) > 10:
            history = history[-10:]

        history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(history_file, "w") as f:
            json.dump(history, f, indent=2)

        logger.info(f"Saved evaluation history to {history_file}")
