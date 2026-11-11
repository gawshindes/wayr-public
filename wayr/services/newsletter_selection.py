"""Story selection for a newsletter issue: thresholds, not a fixed count.

Each story was scored independently by the ImportanceAgent. Selection is
thresholding — include everything above a bar — so the issue is naturally
short on a slow day and long on a big one. Fixed counts survive only as tail
guardrails: a floor so a slow day never ships a two-story issue, a ceiling so
a huge day never ships forty. Overflow from the ceiling is demoted into the
roundup rather than dropped.

Pure functions over tracker dicts, so the whole policy is unit-testable.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

Candidate = Dict[str, Any]


def score_of(candidate: Candidate) -> Optional[float]:
    imp = candidate.get("importance")
    if isinstance(imp, dict) and isinstance(imp.get("score"), (int, float)):
        return float(imp["score"])
    return None


@dataclass
class Selection:
    featured: List[Candidate] = field(default_factory=list)  # full treatment
    roundup: List[Candidate] = field(default_factory=list)  # bare link
    dropped: List[Candidate] = field(default_factory=list)  # below roundup bar
    unscored: List[Candidate] = field(default_factory=list)  # importance never ran
    backfilled: int = 0  # promoted into featured to reach the floor
    demoted: int = 0  # pushed into roundup to respect the ceiling

    @property
    def counts(self) -> Dict[str, int]:
        return {
            "featured": len(self.featured),
            "roundup": len(self.roundup),
            "dropped": len(self.dropped),
            "unscored": len(self.unscored),
            "backfilled": self.backfilled,
            "demoted": self.demoted,
        }


def select_stories(
    candidates: List[Candidate],
    *,
    feature_threshold: float,
    roundup_threshold: float,
    min_stories: int,
    max_stories: int,
) -> Selection:
    """Partition candidates into tiers, then apply the two guardrails.

    Candidates are expected sorted by score descending; ordering is preserved
    within each tier so the compile step sees the best first.
    """
    sel = Selection()

    scored = [(score_of(c), c) for c in candidates]
    sel.unscored = [c for s, c in scored if s is None]
    ranked = sorted([(s, c) for s, c in scored if s is not None], key=lambda x: -x[0])

    for s, c in ranked:
        if s >= feature_threshold:
            sel.featured.append(c)
        elif s >= roundup_threshold:
            sel.roundup.append(c)
        else:
            sel.dropped.append(c)

    # Floor: never ship a two-story issue on a slow day. Promote the best of
    # the roundup tier until the floor is met (or the roundup is exhausted).
    while len(sel.featured) < min_stories and sel.roundup:
        sel.featured.append(sel.roundup.pop(0))
        sel.backfilled += 1

    # Ceiling: a guardrail, not editorial policy. Demote the weakest featured
    # stories to the *front* of the roundup so they still lead it.
    if len(sel.featured) > max_stories:
        overflow = sel.featured[max_stories:]
        sel.featured = sel.featured[:max_stories]
        sel.roundup = overflow + sel.roundup
        sel.demoted = len(overflow)

    return sel
