"""Onboard a newsletter: turn a few answers into a `newsletters/<slug>/` folder.

The folder is the whole configuration — description, sources, criteria,
sections, voice, env.overrides — so onboarding is "write those six files from
what the customer told us", starting from the demo that is closest in shape.
The answers can come from CLI flags, interactive prompts, or a JSON file (the
shape a UI form would post), and the result is reviewable text, never state.

Nothing here talks to the network: sources are written as given, and the next
step the command prints is `./demo.sh <slug> check`.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from wayr.models.sources import parse_sources_text

NEWSLETTERS_DIR = Path("newsletters")

# How often it goes out → how old a story may be and how many it needs. The
# freshness cap must clear the longest gap between issues (a weekday daily's is
# the weekend), which is why "daily" is not 24.
CADENCES: Dict[str, dict] = {
    "weekdays": {"max_story_age_hours": 96, "min_stories": 5, "max_stories": 15},
    "daily": {"max_story_age_hours": 36, "min_stories": 5, "max_stories": 15},
    "weekly": {"max_story_age_hours": 240, "min_stories": 7, "max_stories": 15},
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


@dataclass
class Answers:
    slug: str
    name: str
    description: str
    readers: str
    cadence: str = "weekdays"
    sources: List[str] = field(default_factory=list)
    matters: List[str] = field(default_factory=list)  # what matters most, one per line
    avoid: List[str] = field(default_factory=list)  # what matters less
    industry: Optional[str] = None
    categories: Optional[List[str]] = None
    category_name: Optional[str] = None  # WordPress category the draft lands in
    voice_notes: str = ""  # free text appended to the copied voice guide

    def validate(self) -> None:
        if not _SLUG.match(self.slug):
            raise ValueError(
                f"slug {self.slug!r}: lowercase letters, digits and dashes, 2–41 chars"
            )
        if self.cadence not in CADENCES:
            raise ValueError(f"cadence must be one of {', '.join(CADENCES)}")
        if not self.name.strip() or not self.readers.strip():
            raise ValueError("name and readers are required")
        parse_sources_text("\n".join(self.sources), filename="sources")


def _title(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def render_description(a: Answers, from_demo: str) -> str:
    return f"""# {a.name}

{a.description.strip() or "_(one line on what this newsletter is)_"}

Started from the `{from_demo}` demo; the structure and voice were copied from it and
should be edited to fit.

| Field | Value |
|---|---|
| Name | {a.name} |
| Readers | {a.readers.strip()} |
| Cadence | {a.cadence} |
| Sources | {len(a.sources)} in `sources.txt` — run `./demo.sh {a.slug} check` |
| Drafts land in | staging.wayrtoday.com drafts, category `{a.category_name or 'Newsletter - ' + a.name}` |

Files: `sources.txt`, `criteria.md`, `sections.yaml`, `voice.md`, `env.overrides`.
"""


def render_sources(a: Answers) -> str:
    head = (
        "# Sources — one per line: URL [type=rss|page] [origin=press|brand]  # comment\n"
        "# Written by onboarding as given; validate with: ./demo.sh "
        f"{a.slug} check\n"
    )
    return head + "\n".join(a.sources) + ("\n" if a.sources else "")


def render_criteria(a: Answers) -> str:
    matters = "\n".join(f"- {m}" for m in a.matters) or (
        "- _(what should score high — launches, rulings, deals, incidents…)_"
    )
    avoid = "\n".join(f"- {m}" for m in a.avoid) or (
        "- _(what should score low — opinion, minor versions, off-topic…)_"
    )
    return f"""# Importance criteria — {a.name}

## Who reads this
{a.readers.strip()}

## What matters most (score high)
{matters}

## What matters less (score low)
{avoid}

## Calibration
Use the whole scale. A typical issue has one or two stories at 0.8 or above and most
at 0.3–0.5; if everything lands at 0.5 the score is not doing its job.
- 0.9–1.0: the story every reader will be asked about.
- 0.7–0.8: clearly among the most consequential for these readers.
- 0.5–0.6: worth a line in the roundup.
- 0.2–0.4: real but minor, or for a different reader.
- 0.0–0.1: off-topic, opinion, or a repeat.
"""


def render_env(a: Answers, from_demo_env: str, from_demo: str = "") -> str:
    """Start from the demo's env.overrides, rewrite identity, paths and cadence.
    Comment lines that name the demo's slug get the new one too."""
    c = CADENCES[a.cadence]
    lines = []
    seen = set()
    replacements = {
        "PLATFORM_NAME": a.name,
        "RSS_FEEDS_FILE": f"newsletters/{a.slug}/sources.txt",
        "NEWSLETTER_SECTIONS_FILE": f"newsletters/{a.slug}/sections.yaml",
        "NEWSLETTER_IMPORTANCE_CRITERIA_FILE": f"newsletters/{a.slug}/criteria.md",
        "NEWSLETTER_VOICE_FILE": f"newsletters/{a.slug}/voice.md",
        "NEWSLETTER_WORDPRESS_CATEGORY": a.category_name or f"Newsletter - {a.name}",
        "ARTICLE_CACHE_FILE": f"data/{a.slug}/processed_articles.json",
        "FEED_LEDGER_FILE": f"data/{a.slug}/feed_ledger.json",
        "NEWSLETTER_RUNS_FILE": f"data/{a.slug}/newsletter_runs.json",
        "WP_TAXONOMY_CACHE_FILE": f"data/{a.slug}/wp_taxonomy.json",
        "REGION": a.slug,
        "NEWSLETTER_MAX_STORY_AGE_HOURS": str(c["max_story_age_hours"]),
        "NEWSLETTER_MIN_STORIES": str(c["min_stories"]),
        "NEWSLETTER_MAX_STORIES": str(c["max_stories"]),
    }
    if a.industry:
        replacements["INDUSTRY"] = a.industry
    if a.categories:
        replacements["INDUSTRY_CATEGORIES"] = ",".join(a.categories)
    for raw in from_demo_env.splitlines():
        line = raw
        if "=" in raw and not raw.lstrip().startswith("#"):
            key = raw.split("=", 1)[0].strip()
            if key in replacements:
                line = f"{key}={replacements[key]}"
                seen.add(key)
        if raw.startswith("# ----"):
            line = f"# ---- {a.name} — appended to .env.demo-<name> by demo.sh; later keys win ----"
        elif raw.lstrip().startswith("#") and from_demo:
            line = raw.replace(from_demo, a.slug)
        lines.append(line)
    for key, val in replacements.items():
        if key not in seen and key not in ("INDUSTRY", "INDUSTRY_CATEGORIES"):
            lines.append(f"{key}={val}")
    return "\n".join(lines) + "\n"


def build(a: Answers, from_demo: str, root: Path = NEWSLETTERS_DIR) -> Dict[str, str]:
    """Return {relative path: content} for the new folder. Pure; writes nothing."""
    a.validate()
    src = root / from_demo
    if not (src / "env.overrides").is_file():
        raise FileNotFoundError(f"no demo to start from at {src}")
    files: Dict[str, str] = {
        "description.md": render_description(a, from_demo),
        "sources.txt": render_sources(a),
        "criteria.md": render_criteria(a),
        "env.overrides": render_env(
            a, (src / "env.overrides").read_text(encoding="utf-8"), from_demo
        ),
    }
    sections = src / "sections.yaml"
    files["sections.yaml"] = (
        sections.read_text(encoding="utf-8")
        if sections.is_file()
        else "sections:\n  - name: Top stories\n    type: stories\n    style: feature\n    tier: feature\n    max: 5\n  - name: Also worth knowing\n    type: stories\n    style: link_only\n    tier: roundup\n    max: 8\n"
    )
    voice = src / "voice.md"
    voice_text = (
        voice.read_text(encoding="utf-8")
        if voice.is_file()
        else Path(__file__)
        .resolve()
        .parent.joinpath("templates", "voice.default.md")
        .read_text(encoding="utf-8")
    )
    if a.voice_notes.strip():
        voice_text = (
            voice_text.rstrip("\n")
            + f"\n\n## Notes from onboarding\n{a.voice_notes.strip()}\n"
        )
    files["voice.md"] = voice_text
    return files


def write(
    files: Dict[str, str],
    slug: str,
    root: Path = NEWSLETTERS_DIR,
    overwrite: bool = False,
) -> Path:
    dest = root / slug
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} already exists (use --overwrite to replace)")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel, content in files.items():
        (dest / rel).write_text(content, encoding="utf-8")
    return dest


def next_steps(slug: str) -> str:
    return (
        f"./demo.sh {slug} check      # do the sources deliver stories?\n"
        f"./demo.sh {slug} prework    # discover → classify → score\n"
        f"./demo.sh {slug} dump       # read the scores; tune criteria.md\n"
        f"./demo.sh {slug} preview    # the issue as HTML, nothing pushed\n"
        f"./demo.sh {slug} draft      # a draft on staging"
    )
