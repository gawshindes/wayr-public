"""Pytest configuration and fixtures."""

import pytest
import os
from datetime import datetime
from wayr.models import RSSArticle, ClassifiedArticle
from wayr.models.articles import ContentType


@pytest.fixture
def sample_rss_article():
    """Sample RSSArticle for testing."""
    return RSSArticle(
        title="Breaking: New AI Model Achieves Record Performance",
        url="https://techcrunch.com/2024/01/15/new-ai-model",
        source="TechCrunch",
        published_date=datetime(2024, 1, 15, 10, 30),
        summary="A groundbreaking AI model has achieved unprecedented results...",
        author="Jane Smith",
        tags=["AI", "Machine Learning"],
    )


@pytest.fixture
def sample_classified_article(sample_rss_article):
    """Sample ClassifiedArticle for testing."""
    return ClassifiedArticle(
        original=sample_rss_article,
        content_type=ContentType.NEWS,
        content_type_confidence=0.95,
        content_type_reason="Reports on AI model performance",
        categories=["AI"],
        confidence=0.95,
        classification_reason="Article discusses AI models and machine learning performance",
    )


@pytest.fixture
def mock_openai_key():
    """Mock OpenAI API key."""
    return "sk-test-key-123"


@pytest.fixture
def mock_wordpress_config():
    """Mock WordPress configuration."""
    return {
        "url": "https://wayr.today",
        "username": "test_user",
        "app_password": "test password 1234",
    }
