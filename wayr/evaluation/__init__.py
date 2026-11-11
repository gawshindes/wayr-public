"""Evaluation module for WAYR platform quality assurance."""

from wayr.evaluation.classification.eval import ClassificationEvaluator
from wayr.evaluation.industry_classifier.eval import IndustryClassifierEvaluator
from wayr.evaluation.news_classifier.eval import NewsClassifierEvaluator
from wayr.evaluation.category_classifier.eval import CategoryClassifierEvaluator
from wayr.evaluation.metrics import (
    AgentEvaluationResult,
    AgentMetrics,
    AuthorMetrics,
    CategoryClassifierAgentMetrics,
    ClassificationMetrics,
    DeduplicationMetrics,
    EvaluationResult,
    IndustryClassifierAgentMetrics,
    NewsClassifierAgentMetrics,
    ProofreaderMetrics,
    SummaryMetrics,
)
from wayr.evaluation.author.eval import AuthorEvaluator
from wayr.evaluation.dedup.eval import DeduplicationEvaluator
from wayr.evaluation.proofreader.eval import ProofreaderEvaluator
from wayr.evaluation.summary.eval import SummaryEvaluator

__all__ = [
    "ClassificationEvaluator",
    "IndustryClassifierEvaluator",
    "NewsClassifierEvaluator",
    "CategoryClassifierEvaluator",
    "AuthorEvaluator",
    "DeduplicationEvaluator",
    "ProofreaderEvaluator",
    "SummaryEvaluator",
    "AgentEvaluationResult",
    "AgentMetrics",
    "AuthorMetrics",
    "CategoryClassifierAgentMetrics",
    "ClassificationMetrics",
    "DeduplicationMetrics",
    "EvaluationResult",
    "IndustryClassifierAgentMetrics",
    "NewsClassifierAgentMetrics",
    "ProofreaderMetrics",
    "SummaryMetrics",
]
