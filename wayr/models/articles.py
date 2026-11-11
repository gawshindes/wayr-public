"""Article data models for the WAYR platform."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, HttpUrl, model_validator


class ContentType(str, Enum):
    """Content type classification.

    NEWS = reporting on a new, significant event.
    DEAL, OPINION, REPORT, REVIEW, OTHER = non-news subcategories (see Agent B).
    """

    NEWS = "news"
    DEAL = "deal"
    OPINION = "opinion"
    REPORT = "report"
    REVIEW = "review"
    OTHER = "other"


_LEGACY_CONTENT_TYPES = {"guide": "other"}


class RSSArticle(BaseModel):
    """Raw article extracted from RSS feed."""

    title: str = Field(..., description="Article title from RSS feed")
    url: HttpUrl = Field(..., description="Original article URL")
    source: str = Field(..., description="Source publication name")
    published_date: datetime = Field(..., description="Publication date")
    summary: Optional[str] = Field(None, description="RSS feed summary/description")
    content: Optional[str] = Field(
        None, description="Full article content if extracted"
    )
    author: Optional[str] = Field(None, description="Article author")
    tags: List[str] = Field(default_factory=list, description="Tags from RSS feed")
    rss_raw: Optional[dict] = Field(
        None,
        description="JSON-serializable snapshot of the original feedparser entry, preserved for Supabase audit.",
    )
    feed_url: Optional[str] = Field(
        None, description="The configured source URL this article came from"
    )
    feed_content: Optional[str] = Field(
        None,
        description=(
            "The article text the feed itself carried (content:encoded or a full "
            "description), cleaned to plain text. Used when the page cannot be "
            "fetched — Medium blogs and bot-protected newsrooms."
        ),
    )
    source_origin: Optional[str] = Field(
        None,
        description=(
            "Whose words these are, from the sources file: 'press' (a news site "
            "covering events) or 'brand' (the company announcing its own). None when unknown."
        ),
    )

    class Config:
        json_schema_extra = {
            "example": {
                "title": "New AI Model Breaks Performance Records",
                "url": "https://example.com/ai-model-news",
                "source": "TechCrunch",
                "published_date": "2024-01-15T10:30:00Z",
                "summary": "A breakthrough in AI research...",
                "author": "Jane Smith",
                "tags": ["AI", "Machine Learning"],
            }
        }


class IndustryFilterResult(BaseModel):
    """Result from the industry classifier."""

    belongs_to_industry: bool = Field(
        ..., description="Whether the article's primary domain is the target industry"
    )
    reasoning: str = Field(..., description="1-3 sentence explanation")


class NewsTypeResult(BaseModel):
    """Result from Agent B (news vs non-news + subcategory)."""

    content_type: ContentType = Field(
        ..., description="NEWS or one of the non-news subcategories"
    )
    reasoning: str = Field(..., description="Explanation of the classification")


class CategoryResult(BaseModel):
    """Result from Agent C (primary + secondary news category)."""

    primary: str = Field(..., description="Primary news category")
    secondary: str = Field(..., description="Secondary news category")
    reasoning: str = Field(..., description="Justification for category choices")


class ImportanceResult(BaseModel):
    """Output of the ImportanceAgent for one news story."""

    score: float = Field(
        ..., ge=0.0, le=1.0, description="Importance to the newsletter's readers, 0-1"
    )
    reasoning: str = Field(default="", description="The decisive factor")
    parsed: bool = Field(
        default=True,
        description="False when the model reply could not be parsed and a neutral score was recorded",
    )


class ClassifiedArticle(BaseModel):
    """Article with combined classification from Agents A/B/C.

    `categories` is ordered (primary first). Empty for non-news articles and for
    articles that fail the industry filter.
    """

    original: RSSArticle = Field(..., description="Original RSS article data")

    # Agent A output
    belongs_to_industry: bool = Field(
        default=True, description="Passed the industry filter"
    )
    industry_reasoning: Optional[str] = Field(
        default=None, description="Why it does/doesn't belong to the industry"
    )
    industry_detected: Optional[str] = Field(
        default=None,
        description="Industry the classifier thought the article belonged to (populated on reject)",
    )

    # Agent B output (L1)
    content_type: ContentType = Field(..., description="Content type")
    content_type_confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Content type confidence score"
    )
    content_type_reason: str = Field(
        default="", description="Why this content type was chosen"
    )

    # Agent C output (L2) — only populated for NEWS articles
    categories: List[str] = Field(
        default_factory=list,
        description="News categories from configured list (primary first)",
    )
    category_confidences: Optional[List[float]] = Field(
        None, description="Confidence scores for each category"
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Primary category confidence score"
    )
    classification_reason: str = Field(
        default="", description="Why these categories were chosen"
    )

    # ImportanceAgent output — newsletter mode only, populated after the
    # category stage on news stories. None until scored.
    importance_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Importance to the newsletter's readers",
    )
    importance_reason: Optional[str] = Field(
        default=None, description="Why the importance score was given"
    )

    @property
    def category(self) -> Optional[str]:
        """Primary category (first in the list), or None for non-news."""
        return self.categories[0] if self.categories else None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, data):
        """Coerce legacy cache/test data into the new shape."""
        if not isinstance(data, dict):
            return data

        # Legacy single `category` → `categories` list
        if "category" in data and "categories" not in data:
            cat = data.pop("category")
            data["categories"] = [cat] if cat else []

        # Legacy content types (e.g. "guide" → "other")
        ct = data.get("content_type")
        if isinstance(ct, str) and ct in _LEGACY_CONTENT_TYPES:
            data["content_type"] = _LEGACY_CONTENT_TYPES[ct]

        # Coerce enum-valued categories (from old serialized data) to strings
        cats = data.get("categories")
        if isinstance(cats, list):
            data["categories"] = [
                c.value if isinstance(c, Enum) else str(c)
                for c in cats
                if c is not None
            ]

        return data

    class Config:
        json_schema_extra = {
            "example": {
                "original": {"title": "...", "url": "..."},
                "belongs_to_industry": True,
                "content_type": "news",
                "content_type_confidence": 0.98,
                "content_type_reason": "Reports on a new product announcement",
                "categories": ["AI", "Startup"],
                "confidence": 0.95,
                "classification_reason": "Primary: AI. Secondary: Startup.",
            }
        }


class GeneratedArticle(BaseModel):
    """AI-generated article content."""

    classified: ClassifiedArticle = Field(..., description="Classified article data")

    generated_title: str = Field(..., description="AI-generated article title")
    generated_content: str = Field(
        ..., description="AI-generated article content", min_length=200
    )
    sources_cited: List[str] = Field(
        ..., description="List of source URLs cited in the article"
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when article was generated",
    )
    model_used: str = Field(default="gpt-4", description="AI model used for generation")

    class Config:
        json_schema_extra = {
            "example": {
                "classified": {"original": {"title": "..."}, "categories": ["AI"]},
                "generated_title": "Breakthrough AI Model Achieves New Benchmarks",
                "generated_content": "In recent developments in artificial intelligence...",
                "sources_cited": ["https://example.com/source1"],
                "model_used": "gpt-4",
            }
        }


class EditedArticle(BaseModel):
    """Article that has been proofread and edited by a second AI model."""

    generated: GeneratedArticle = Field(
        ..., description="Original generated article data"
    )

    edited_title: str = Field(..., description="Proofread and edited title")
    edited_content: str = Field(
        ..., description="Proofread and edited article content", min_length=200
    )
    edit_summary: str = Field(..., description="Summary of changes made during editing")
    editor_model: str = Field(
        default="claude-3-5-sonnet-20241022", description="AI model used for editing"
    )
    edited_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when article was edited",
    )

    summary: Optional[str] = Field(
        None,
        description="LLM-generated summary for SEO meta description and social media",
    )

    corrected_categories: Optional[List[str]] = Field(
        None,
        description="Corrected categories if proofreader determines the original classification was wrong",
    )
    category_correction_reason: Optional[str] = Field(
        None, description="Explanation for why the categories were corrected"
    )

    @property
    def corrected_category(self) -> Optional[str]:
        """Primary corrected category. Backward compatible."""
        return self.corrected_categories[0] if self.corrected_categories else None

    @model_validator(mode="before")
    @classmethod
    def normalize_corrected_category_format(cls, data):
        """Accept old-style single `corrected_category` and coerce enum values."""
        if not isinstance(data, dict):
            return data
        if "corrected_category" in data and "corrected_categories" not in data:
            val = data.pop("corrected_category")
            data["corrected_categories"] = [val] if val else None
        cats = data.get("corrected_categories")
        if isinstance(cats, list):
            data["corrected_categories"] = [
                c.value if isinstance(c, Enum) else str(c)
                for c in cats
                if c is not None
            ]
        return data

    class Config:
        json_schema_extra = {
            "example": {
                "generated": {"classified": {...}, "generated_title": "..."},
                "edited_title": "AI Model Breakthrough: Record-Breaking Performance Achieved",
                "edited_content": "In a significant development for artificial intelligence...",
                "edit_summary": "Improved clarity, fixed grammar, enhanced flow",
                "editor_model": "claude-3-5-sonnet-20241022",
            }
        }


class PublishedArticle(BaseModel):
    """Article that has been published to WordPress."""

    edited: EditedArticle = Field(..., description="Edited article data")

    wordpress_post_id: int = Field(..., description="WordPress post ID")
    wordpress_url: HttpUrl = Field(..., description="Published article URL")
    published_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when article was published",
    )
    status: str = Field(default="publish", description="WordPress post status")

    class Config:
        json_schema_extra = {
            "example": {
                "edited": {"generated": {...}, "edited_title": "..."},
                "wordpress_post_id": 12345,
                "wordpress_url": "https://wayr.today/breakthrough-ai-model",
                "status": "publish",
            }
        }
