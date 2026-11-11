"""Assign selected stories into the configured sections.

Deterministic Python, not the LLM's job. Two passes, both in schema order
with the highest score first, and a story appears once:

1. Strict: each story lands in the first section whose tier and category
   filter it satisfies and which still has room.
2. Fill: whatever is still unplaced takes remaining room in any stories
   section whose category filter accepts it, tier ignored. A section's tier is
   its *preference*, not a gate — B1's first dump had 9 feature-tier slots
   empty and 22 roundup stories unplaced, which no editor would ship. The
   section's style governs how a promoted story renders.

Anything with no home after both passes is reported as `unplaced` so it is
never silently lost.

Empty stories sections are omitted (the render would be a bare heading);
placeholders are always kept so the editor sees the full skeleton.
"""

from dataclasses import dataclass, field
from typing import List

from wayr.models.newsletter import (
    NewsletterSection,
    NewsletterStory,
    SectionSpec,
    SectionType,
)


@dataclass
class Assignment:
    sections: List[NewsletterSection] = field(default_factory=list)
    unplaced: List[NewsletterStory] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return sum(len(s.stories) for s in self.sections)


def assign_stories(
    specs: List[SectionSpec],
    featured: List[NewsletterStory],
    roundup: List[NewsletterStory],
) -> Assignment:
    """`specs` should already be filtered for the day (SectionSchema.for_day)."""
    sections = [NewsletterSection(spec=s) for s in specs]

    def place(story: NewsletterStory, strict_tier: bool) -> bool:
        for sec in sections:
            spec = sec.spec
            if spec.type != SectionType.STORIES:
                continue
            if strict_tier and spec.tier != story.tier:
                continue
            if not spec.accepts_category(story.category):
                continue
            if spec.max is not None and len(sec.stories) >= spec.max:
                continue
            sec.stories.append(story)
            return True
        return False

    ordered = list(featured) + list(roundup)
    leftover = [s for s in ordered if not place(s, strict_tier=True)]
    unplaced = [s for s in leftover if not place(s, strict_tier=False)]

    kept = [s for s in sections if s.is_placeholder or s.stories]
    return Assignment(sections=kept, unplaced=unplaced)
