"""Newsletter structure models: section schema, stories, sections, issue.

The section schema is deliberately plain declarative data — it is one of the
two artifacts onboarding will emit (the other is the feed list), so it must
stay serializable config with no logic. Assignment and rendering live in
services; these models only describe shape and validate it.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TEMPLATES = Path(__file__).parent.parent / "templates"
DEFAULT_SECTIONS_FILE = _TEMPLATES / "newsletter_sections_default.yaml"


class ItemStyle(str, Enum):
    """How an item is rendered — and, crucially, where its link sits."""

    FEATURE = "feature"  # written headline (unlinked), blurb, link on the source name
    BRIEF = "brief"  # one line, link on the anchor phrase inline
    QUICK_HIT = "quick_hit"  # emoji + linked written headline, nothing else
    LINK_ONLY = "link_only"  # the source's own title, linked, nothing else


class SectionType(str, Enum):
    STORIES = "stories"  # filled by the pipeline
    PLACEHOLDER = "placeholder"  # heading + empty block for the editor


class Tier(str, Enum):
    FEATURE = "feature"
    ROUNDUP = "roundup"


class SectionSpec(BaseModel):
    """One entry in the section schema."""

    name: str = Field(..., min_length=1)
    type: SectionType = SectionType.STORIES
    style: Optional[ItemStyle] = None
    max: Optional[int] = Field(default=None, ge=1)
    tier: Optional[Tier] = None
    categories: Optional[List[str]] = None  # None or ["*"] → any category
    days: Optional[List[str]] = None  # None → every day; else ["mon", "thu"]
    show_source: Optional[bool] = None  # None → by style (feature shows it)
    show_read_time: bool = False  # "(N minute read)" after the headline — TLDR's tag
    commentary: bool = (
        True  # feature stories get a "why it matters"; false = blurb only
    )

    @model_validator(mode="after")
    def _check(self) -> "SectionSpec":
        if self.type == SectionType.STORIES:
            if self.style is None:
                raise ValueError(
                    f"section '{self.name}': stories sections need a style"
                )
            if self.tier is None:
                raise ValueError(f"section '{self.name}': stories sections need a tier")
        if self.days:
            bad = [d for d in self.days if d.lower() not in WEEKDAYS]
            if bad:
                raise ValueError(
                    f"section '{self.name}': unknown days {bad}; use {list(WEEKDAYS)}"
                )
            self.days = [d.lower() for d in self.days]
        return self

    @property
    def is_placeholder(self) -> bool:
        return self.type == SectionType.PLACEHOLDER

    @property
    def shows_source(self) -> bool:
        if self.show_source is not None:
            return self.show_source
        return self.style == ItemStyle.FEATURE

    def active_on(self, weekday: int) -> bool:
        """`weekday` as datetime.weekday(): 0 = Monday."""
        return not self.days or WEEKDAYS[weekday] in self.days

    def accepts_category(self, category: Optional[str]) -> bool:
        if not self.categories or "*" in self.categories:
            return True
        return category is not None and category.lower() in {
            c.lower() for c in self.categories
        }


class SectionSchema(BaseModel):
    sections: List[SectionSpec] = Field(..., min_length=1)
    # Structure lives here, not in the voice guide: a model asked in prose to
    # "write no intro" still writes one. `intro: false` means the issue has no
    # opening at all — the compile is told, and the renderer gets nothing.
    intro: bool = True

    @model_validator(mode="after")
    def _check(self) -> "SectionSchema":
        names = [s.name for s in self.sections]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate section names: {dupes}")
        if not any(s.type == SectionType.STORIES for s in self.sections):
            raise ValueError("schema needs at least one stories section")
        return self

    @classmethod
    def load(cls, path: Optional[str] = None) -> "SectionSchema":
        """Load from YAML; `None` → the packaged generic default."""
        file = Path(path) if path else DEFAULT_SECTIONS_FILE
        if not file.is_file():
            raise FileNotFoundError(f"section schema not found: {file}")
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "sections" not in data:
            raise ValueError(f"{file}: expected a top-level 'sections' list")
        return cls(**data)

    def for_day(self, weekday: int) -> List[SectionSpec]:
        return [s for s in self.sections if s.active_on(weekday)]


class NewsletterStory(BaseModel):
    """One story as it appears in an issue.

    The written fields (emoji, headline, blurb, commentary, anchor_text) are
    filled by the compile step. All are optional: the renderer falls back to
    the source title and omits what is missing, so an issue can always ship.
    """

    url_hash: str
    url: str
    source: str
    source_title: str
    category: Optional[str] = None
    score: Optional[float] = None
    tier: Tier

    emoji: Optional[str] = None
    headline: Optional[str] = None
    blurb: Optional[str] = None
    commentary: Optional[str] = None
    anchor_text: Optional[str] = None
    read_minutes: Optional[int] = (
        None  # from the source text, set when content is fetched
    )

    @property
    def display_headline(self) -> str:
        return (self.headline or "").strip() or self.source_title

    @property
    def display_anchor(self) -> str:
        return (self.anchor_text or "").strip() or self.display_headline

    @classmethod
    def from_candidate(cls, d: Dict[str, Any], tier: Tier) -> "NewsletterStory":
        """Build from a tracker candidate dict (see ArticleTracker cache shape)."""
        cat = (d.get("category") or {}).get("primary") or None
        imp = d.get("importance") or {}
        score = imp.get("score") if isinstance(imp.get("score"), (int, float)) else None
        return cls(
            url_hash=d.get("url_hash", ""),
            url=d.get("url", ""),
            source=d.get("source", "") or "",
            source_title=d.get("title", "") or "",
            category=cat,
            score=float(score) if score is not None else None,
            tier=tier,
        )


class NewsletterSection(BaseModel):
    spec: SectionSpec
    stories: List[NewsletterStory] = Field(default_factory=list)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_placeholder(self) -> bool:
        return self.spec.is_placeholder

    @property
    def style(self) -> Optional[str]:
        return self.spec.style.value if self.spec.style else None


class NewsletterIssue(BaseModel):
    title: str
    subject_line: str
    intro: str
    date_label: str
    sections: List[NewsletterSection]
    generated_at: datetime
    llm_generated: bool = False
    platform_name: str
    ai_disclosure_notice: str = ""

    @property
    def story_count(self) -> int:
        return sum(len(s.stories) for s in self.sections)
