"""Tests for data models."""

from datetime import datetime, timezone
from wayr.models import (
    RSSArticle,
    ClassifiedArticle,
    GeneratedArticle,
    PublishedArticle,
)
from wayr.models.articles import ContentType, EditedArticle


def test_rss_article_creation():
    """Test RSSArticle model creation."""
    article = RSSArticle(
        title="Test Article",
        url="https://example.com/article",
        source="Test Source",
        published_date=datetime.now(timezone.utc),
        summary="Test summary",
        tags=["tech", "ai"],
    )

    assert article.title == "Test Article"
    assert article.source == "Test Source"
    assert len(article.tags) == 2


def test_classified_article_creation():
    """Test ClassifiedArticle model creation with categories list."""
    rss_article = RSSArticle(
        title="AI Breakthrough",
        url="https://example.com/ai",
        source="Tech News",
        published_date=datetime.now(timezone.utc),
    )

    classified = ClassifiedArticle(
        original=rss_article,
        content_type=ContentType.NEWS,
        content_type_confidence=0.95,
        content_type_reason="Reports on a new AI development",
        categories=["AI"],
        confidence=0.95,
        classification_reason="Article discusses AI technology",
    )

    assert classified.category == "AI"
    assert classified.categories == ["AI"]
    assert classified.confidence == 0.95
    assert classified.content_type == ContentType.NEWS
    assert classified.original.title == "AI Breakthrough"


def test_classified_article_multi_category():
    """Test ClassifiedArticle with multiple categories."""
    rss_article = RSSArticle(
        title="OpenAI raises $6.6B in funding",
        url="https://example.com/openai",
        source="TechCrunch",
        published_date=datetime.now(timezone.utc),
    )

    classified = ClassifiedArticle(
        original=rss_article,
        content_type=ContentType.NEWS,
        content_type_confidence=0.95,
        content_type_reason="Reports on funding round",
        categories=["Startup", "AI"],
        category_confidences=[0.9, 0.8],
        confidence=0.9,
        classification_reason="Primary: Startup (funding). Secondary: AI (OpenAI's domain).",
    )

    assert classified.category == "Startup"
    assert classified.categories == ["Startup", "AI"]
    assert classified.category_confidences == [0.9, 0.8]


def test_classified_article_backward_compat():
    """Test ClassifiedArticle accepts old-style single category field."""
    rss_article = RSSArticle(
        title="Test",
        url="https://example.com/test",
        source="Test",
        published_date=datetime.now(timezone.utc),
    )

    classified = ClassifiedArticle(
        original=rss_article,
        content_type=ContentType.NEWS,
        content_type_confidence=0.9,
        content_type_reason="Test",
        category="AI",
        confidence=0.9,
        classification_reason="Test",
    )

    assert classified.category == "AI"
    assert classified.categories == ["AI"]


def test_generated_article_creation():
    """Test GeneratedArticle model creation."""
    rss_article = RSSArticle(
        title="Test",
        url="https://example.com/test",
        source="Test",
        published_date=datetime.now(timezone.utc),
    )

    classified = ClassifiedArticle(
        original=rss_article,
        content_type=ContentType.NEWS,
        content_type_confidence=0.9,
        content_type_reason="Test news article",
        categories=["AI"],
        confidence=0.9,
        classification_reason="Test",
    )

    generated = GeneratedArticle(
        classified=classified,
        generated_title="Generated Title",
        generated_content="This is generated content with more than 200 characters. "
        * 5,
        sources_cited=["https://example.com/test"],
        model_used="gpt-4",
    )

    assert generated.generated_title == "Generated Title"
    assert len(generated.sources_cited) == 1
    assert generated.model_used == "gpt-4"


def test_edited_article_corrected_categories():
    """Test EditedArticle with corrected_categories and backward compat."""
    rss_article = RSSArticle(
        title="Test",
        url="https://example.com/test",
        source="Test",
        published_date=datetime.now(timezone.utc),
    )

    classified = ClassifiedArticle(
        original=rss_article,
        content_type=ContentType.NEWS,
        content_type_confidence=0.9,
        content_type_reason="Test",
        categories=["AI"],
        confidence=0.9,
        classification_reason="Test",
    )

    generated = GeneratedArticle(
        classified=classified,
        generated_title="Title",
        generated_content="This is generated content with more than 200 characters. "
        * 5,
        sources_cited=["https://example.com/test"],
    )

    edited = EditedArticle(
        generated=generated,
        edited_title="Title",
        edited_content="This is edited content with more than 200 characters. " * 5,
        edit_summary="Category corrected",
        editor_model="test",
        corrected_categories=["Startup", "AI"],
        category_correction_reason="Funding story",
    )
    assert edited.corrected_category == "Startup"
    assert edited.corrected_categories == ["Startup", "AI"]

    edited_no_correction = EditedArticle(
        generated=generated,
        edited_title="Title",
        edited_content="This is edited content with more than 200 characters. " * 5,
        edit_summary="No changes",
        editor_model="test",
    )
    assert edited_no_correction.corrected_category is None
    assert edited_no_correction.corrected_categories is None

    edited_old = EditedArticle(
        generated=generated,
        edited_title="Title",
        edited_content="This is edited content with more than 200 characters. " * 5,
        edit_summary="Category corrected",
        editor_model="test",
        corrected_category="Hardware",
    )
    assert edited_old.corrected_category == "Hardware"
    assert edited_old.corrected_categories == ["Hardware"]


def test_published_article_creation():
    """Test PublishedArticle model creation."""
    rss_article = RSSArticle(
        title="Test",
        url="https://example.com/test",
        source="Test",
        published_date=datetime.now(timezone.utc),
    )

    classified = ClassifiedArticle(
        original=rss_article,
        content_type=ContentType.NEWS,
        content_type_confidence=0.9,
        content_type_reason="Test news article",
        categories=["AI"],
        confidence=0.9,
        classification_reason="Test",
    )

    generated = GeneratedArticle(
        classified=classified,
        generated_title="Generated Title",
        generated_content="This is generated content with more than 200 characters. "
        * 5,
        sources_cited=["https://example.com/test"],
    )

    edited = EditedArticle(
        generated=generated,
        edited_title="Edited Title",
        edited_content="This is edited content with more than 200 characters. " * 5,
        edit_summary="Minor edits",
        editor_model="claude-3-haiku",
    )

    published = PublishedArticle(
        edited=edited,
        wordpress_post_id=123,
        wordpress_url="https://wayr.today/article",
        status="publish",
    )

    assert published.wordpress_post_id == 123
    assert published.status == "publish"
    assert str(published.wordpress_url) == "https://wayr.today/article"
