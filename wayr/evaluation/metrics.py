"""Quality metrics models for evaluation."""

from datetime import datetime
from typing import Optional, List, Dict
from pydantic import BaseModel, Field


class ClassificationMetrics(BaseModel):
    """Metrics for classification evaluation."""

    # Basic info
    article_title: str
    article_url: str

    # Classification results
    predicted_content_type: str
    predicted_category: str  # Primary category (backward compat)
    predicted_categories: List[str] = Field(default_factory=list)
    predicted_confidence: float

    # Expected results (from test dataset)
    expected_content_type: Optional[str] = None
    expected_category: Optional[str] = None  # Primary expected
    expected_categories: Optional[List[str]] = None  # Multi-category expected

    # Evaluation
    industry_correct: Optional[bool] = None  # Agent A (industry filter) match
    content_type_correct: Optional[bool] = None
    category_correct: Optional[bool] = None  # Primary category match
    categories_coverage_correct: Optional[bool] = (
        None  # All expected categories present
    )
    confidence_acceptable: bool = Field(
        default=True, description="Confidence >= threshold"
    )

    # Reasons
    classification_reason: str

    # Overall
    passed: bool = Field(default=True, description="Overall pass/fail")
    issues: List[str] = Field(default_factory=list, description="List of issues found")

    class Config:
        json_schema_extra = {
            "example": {
                "article_title": "OpenAI raises $6.6B in funding",
                "article_url": "https://techcrunch.com/...",
                "predicted_content_type": "news",
                "predicted_category": "Startup",
                "predicted_confidence": 0.92,
                "expected_content_type": "news",
                "expected_category": "Startup",
                "content_type_correct": True,
                "category_correct": True,
                "confidence_acceptable": True,
                "classification_reason": "Article reports on funding round",
                "passed": True,
                "issues": [],
            }
        }


class EvaluationResult(BaseModel):
    """Overall evaluation result for a test suite."""

    # Summary
    total_tests: int
    passed_tests: int
    failed_tests: int
    pass_rate: float

    # Per-subagent breakdown
    industry_pass_rate: float = Field(
        default=0.0, description="Agent A (industry filter) pass rate"
    )
    industry_passed: int = Field(default=0, description="Agent A tests passed")
    industry_failed: int = Field(default=0, description="Agent A tests failed")
    l1_pass_rate: float = Field(default=0.0, description="Content type (L1) pass rate")
    l2_pass_rate: float = Field(default=0.0, description="Category (L2) pass rate")
    l1_passed: int = Field(default=0, description="L1 tests passed")
    l1_failed: int = Field(default=0, description="L1 tests failed")
    l2_passed: int = Field(default=0, description="L2 tests passed")
    l2_failed: int = Field(default=0, description="L2 tests failed")

    # Detailed results
    metrics: List[ClassificationMetrics]

    # New detailed metrics
    category_metrics: Dict[str, "CategoryMetrics"] = Field(
        default_factory=dict, description="Per-category metrics"
    )
    confusion_matrix: Optional["ConfusionMatrix"] = Field(
        default=None, description="Confusion matrix"
    )

    # Timestamp
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)

    # Analysis
    common_issues: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)

    def generate_report(self) -> str:
        """Generate a human-readable report."""
        report = []
        report.append("=" * 60)
        report.append("CLASSIFICATION EVALUATION REPORT")
        report.append("=" * 60)
        report.append(f"Total Tests: {self.total_tests}")
        report.append(
            f"Overall: {self.passed_tests}/{self.total_tests} passed ({self.pass_rate:.1%})"
        )
        report.append("")
        report.append("BREAKDOWN BY SUB-AGENT:")
        report.append(
            f"  A (Industry Filter): {self.industry_passed}/"
            f"{self.industry_passed + self.industry_failed} passed "
            f"({self.industry_pass_rate:.1%})"
        )
        report.append(
            f"  B (News vs Non-News): {self.l1_passed}/"
            f"{self.l1_passed + self.l1_failed} passed ({self.l1_pass_rate:.1%})"
        )
        report.append(
            f"  C (Categories): {self.l2_passed}/"
            f"{self.l2_passed + self.l2_failed} passed ({self.l2_pass_rate:.1%})"
        )
        report.append("")

        # Per-category metrics
        if self.category_metrics:
            report.append("DETAILED CATEGORY METRICS:")
            report.append("-" * 60)
            # Header
            report.append(
                f"{'Category':<20} {'Prec':<8} {'Recall':<8} {'F1':<8} {'Count':<8}"
            )
            report.append("-" * 60)

            for cat, m in sorted(self.category_metrics.items()):
                report.append(
                    f"{cat:<20} "
                    f"{m.precision:.2f}    "
                    f"{m.recall:.2f}    "
                    f"{m.f1_score:.2f}    "
                    f"{m.support:<8}"
                )
            report.append("")

        # Confusion Matrix
        if self.confusion_matrix:
            report.append("CONFUSION MATRIX (Rows=Expected, Cols=Predicted):")
            report.append("-" * 60)

            # Get all labels
            labels = self.confusion_matrix.labels

            # Header row
            header = " " * 20
            for label in labels:
                header += f"{label[:8]:<10}"
            report.append(header)

            # Matrix rows
            for i, row_label in enumerate(labels):
                row_str = f"{row_label[:18]:<20}"
                for j, _ in enumerate(labels):
                    val = self.confusion_matrix.matrix[i][j]
                    row_str += f"{val:<10}"
                report.append(row_str)
            report.append("")

        # Failed tests
        if self.failed_tests > 0:
            report.append("FAILED TESTS:")
            report.append("-" * 60)
            for metric in self.metrics:
                if not metric.passed:
                    report.append(f"\n✗ {metric.article_title}")
                    report.append(f"  URL: {metric.article_url}")
                    report.append(
                        f"  Expected: {metric.expected_content_type} / {metric.expected_category}"
                    )
                    report.append(
                        f"  Got: {metric.predicted_content_type} / {metric.predicted_category}"
                    )
                    if metric.issues:
                        report.append(f"  Issues: {', '.join(metric.issues)}")
            report.append("")

        # Common issues
        if self.common_issues:
            report.append("COMMON ISSUES:")
            for issue in self.common_issues:
                report.append(f"  - {issue}")
            report.append("")

        # Recommendations
        if self.recommendations:
            report.append("RECOMMENDATIONS:")
            for rec in self.recommendations:
                report.append(f"  - {rec}")
            report.append("")

        report.append("=" * 60)
        return "\n".join(report)


class CategoryMetrics(BaseModel):
    """Metrics for a specific category."""

    precision: float
    recall: float
    f1_score: float
    support: int  # Number of actual instances


class ConfusionMatrix(BaseModel):
    """Confusion matrix data."""

    labels: List[str]
    matrix: List[List[int]]


# --- Generic metrics for new agent evaluators ---


class AgentMetrics(BaseModel):
    """Base metrics for any agent evaluation."""

    test_case_id: str = ""
    passed: bool = True
    issues: List[str] = Field(default_factory=list)
    checks: Dict[str, bool] = Field(
        default_factory=dict, description="Named check -> pass/fail"
    )


class AgentEvaluationResult(BaseModel):
    """Generic evaluation result for any agent."""

    agent_name: str
    total_tests: int
    passed_tests: int
    failed_tests: int
    pass_rate: float
    check_pass_rates: Dict[str, float] = Field(
        default_factory=dict, description="Per-check pass rates across all tests"
    )
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)
    common_issues: List[str] = Field(default_factory=list)
    model_used: str = ""

    def generate_report(self) -> str:
        """Generate a human-readable report."""
        report = []
        report.append("=" * 60)
        report.append(f"{self.agent_name.upper()} EVALUATION REPORT")
        report.append("=" * 60)
        report.append(f"Model: {self.model_used}")
        report.append(f"Total Tests: {self.total_tests}")
        report.append(
            f"Overall: {self.passed_tests}/{self.total_tests} passed "
            f"({self.pass_rate:.1%})"
        )
        report.append("")

        if self.check_pass_rates:
            report.append("PER-CHECK PASS RATES:")
            report.append("-" * 40)
            for check_name, rate in sorted(self.check_pass_rates.items()):
                report.append(f"  {check_name:<30} {rate:.1%}")
            report.append("")

        if self.common_issues:
            report.append("COMMON ISSUES:")
            for issue in self.common_issues:
                report.append(f"  - {issue}")
            report.append("")

        report.append("=" * 60)
        return "\n".join(report)


class AuthorMetrics(AgentMetrics):
    """Metrics for a single author eval case."""

    article_title: str = ""
    word_count: int = 0
    subheading_count: int = 0
    has_source_citation: bool = False
    banned_adjectives_found: List[str] = Field(default_factory=list)


class ProofreaderMetrics(AgentMetrics):
    """Metrics for a single proofreader eval case."""

    article_title: str = ""
    word_count: int = 0
    banned_adjectives_remaining: List[str] = Field(default_factory=list)
    source_citation_preserved: bool = True
    ai_disclosure_preserved: bool = True
    subheading_count: int = 0
    category_correction_correct: Optional[bool] = None


class SummaryMetrics(AgentMetrics):
    """Metrics for a single summary eval case."""

    article_title: str = ""
    char_count: int = 0
    sentence_count: int = 0


class IndustryClassifierAgentMetrics(AgentMetrics):
    """Metrics for a single industry-classifier eval case."""

    article_title: str = ""
    predicted_belongs: bool = False
    expected_belongs: Optional[bool] = None
    reasoning: str = ""


class NewsClassifierAgentMetrics(AgentMetrics):
    """Metrics for a single news-classifier eval case."""

    article_title: str = ""
    predicted_content_type: str = ""
    expected_content_type: Optional[str] = None
    news_vs_nonnews_correct: Optional[bool] = None
    reasoning: str = ""
    source_origin: Optional[str] = None  # "press" | "brand" from the case, if given


class CategoryClassifierAgentMetrics(AgentMetrics):
    """Metrics for a single category-classifier eval case."""

    article_title: str = ""
    predicted_primary: str = ""
    predicted_secondary: str = ""
    expected_category: Optional[str] = None
    expected_categories: Optional[List[str]] = None
    coverage_correct: Optional[bool] = None
    reasoning: str = ""


class DeduplicationMetrics(AgentMetrics):
    """Metrics for a single dedup eval batch."""

    batch_name: str = ""
    expected_groups: List[List[int]] = Field(default_factory=list)
    predicted_groups: List[List[int]] = Field(default_factory=list)
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
