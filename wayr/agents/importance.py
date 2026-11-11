"""Importance Agent.

Scores how much a confirmed news story matters to a specific newsletter's
readers. Runs in prework after the category stage, only on stories that
survived classification. The score drives newsletter selection: above the
feature threshold gets full treatment, above the roundup threshold gets a
bare link, below is dropped.

The reader criteria block in the prompt is the seam for per-customer tuning.
With no configured criteria a default is derived from the industry; a
customer's onboarding description replaces it through one setting, not a
forked prompt.
"""

import re
from pathlib import Path
from typing import Optional

from wayr.agents.base import BaseAgent
from wayr.models import ClassifiedArticle, ImportanceResult
from wayr.utils import get_logger

logger = get_logger(__name__)

# Neutral score when the model's reply cannot be parsed. Lands in the roundup
# tier rather than silently vanishing, and `parsed=False` flags it in the
# candidate dump.
_UNPARSEABLE_SCORE = 0.5


_TEMPLATES = Path(__file__).parent.parent / "templates"
_DEFAULT_CRITERIA_FILE = _TEMPLATES / "importance_criteria.default.md"
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _clean(text: str) -> str:
    """Strip HTML comments (file-level instructions) and surrounding whitespace."""
    return _COMMENT.sub("", text).strip()


def default_criteria(industry: str) -> str:
    """Packaged reader criteria, used when the customer has not supplied their own."""
    text = _DEFAULT_CRITERIA_FILE.read_text(encoding="utf-8")
    return _clean(text.replace("{{INDUSTRY}}", industry))


def resolve_criteria(
    inline: Optional[str], file_path: Optional[str], industry: str
) -> tuple[str, str]:
    """Pick the reader criteria: inline var → file → packaged default.

    Returns (criteria, source) where source is one of "inline", "file", "default"
    so the orchestrator can log which one is in force.
    """
    if inline and inline.strip():
        return inline.strip(), "inline"
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"NEWSLETTER_IMPORTANCE_CRITERIA_FILE points at {file_path}, "
                "which does not exist"
            )
        text = _clean(path.read_text(encoding="utf-8"))
        if not text:
            raise ValueError(
                f"NEWSLETTER_IMPORTANCE_CRITERIA_FILE {file_path} is empty after "
                "stripping comments"
            )
        return text, "file"
    return default_criteria(industry), "default"


class ImportanceAgent(BaseAgent):
    """Scores a classified news story's importance to the newsletter's readers."""

    def __init__(
        self,
        api_key: str,
        model: str,
        provider: str = "google",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        industry: str = "Technology",
        criteria: Optional[str] = None,
        criteria_file: Optional[str] = None,
    ):
        super().__init__(
            name="ImportanceAgent",
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.industry = industry
        self.criteria, self.criteria_source = resolve_criteria(
            criteria, criteria_file, industry
        )

    async def score(self, article: ClassifiedArticle) -> ImportanceResult:
        """Score one classified news article. Never raises on a bad reply."""
        orig = article.original
        prompt = self.load_prompt(
            "importance",
            ARTICLE_CONTENT=orig.content or "",
            TITLE=orig.title,
            CATEGORY=article.category or "Uncategorized",
            IMPORTANCE_CRITERIA=self.criteria,
        )

        response = await self._call_llm(user_prompt=prompt)
        block = self._extract_tag(response, "importance") or ""

        score_match = re.search(r"Score:\s*([0-9]+(?:\.[0-9]+)?)", block, re.IGNORECASE)
        reasoning_match = re.search(
            r"Reasoning:\s*(.+)", block, re.IGNORECASE | re.DOTALL
        )

        if not score_match:
            logger.warning(
                f"ImportanceAgent: could not parse score for '{orig.title[:60]}...'. "
                f"Recording neutral {_UNPARSEABLE_SCORE} as unparsed."
            )
            return ImportanceResult(
                score=_UNPARSEABLE_SCORE,
                reasoning="Unparseable response from model",
                parsed=False,
            )

        raw = float(score_match.group(1))
        clamped = min(10.0, max(0.0, raw))
        if clamped != raw:
            logger.warning(
                f"ImportanceAgent: score {raw} out of range for '{orig.title[:60]}...', "
                f"clamped to {clamped}."
            )

        reasoning = (
            reasoning_match.group(1).strip()
            if reasoning_match
            else "No reasoning given"
        )
        return ImportanceResult(score=round(clamped / 10.0, 2), reasoning=reasoning)
