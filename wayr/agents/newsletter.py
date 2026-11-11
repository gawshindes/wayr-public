"""Newsletter Agent — the compile call.

One LLM call per issue writes the editorial layer over already-selected,
already-summarized stories: subject line, title, intro, and per-story emoji,
headline and (by style) commentary or anchor phrase. Blurbs are never
rewritten. `link_only` items are pure data and never reach the model.

`compile()` mutates the issue in place and raises on a bad reply, so the
orchestrator can fall back to the deterministic issue built before the call —
the send always ships. A story the model skipped keeps its fallbacks (source
title, no emoji) rather than failing the whole issue.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from wayr.agents.base import BaseAgent
from wayr.models.newsletter import ItemStyle, NewsletterIssue
from wayr.utils import get_logger

logger = get_logger(__name__)

_WRITTEN_STYLES = {ItemStyle.FEATURE, ItemStyle.BRIEF, ItemStyle.QUICK_HIT}
_MAX_HEADLINE_CHARS = 140
_MAX_ANCHOR_CHARS = 60
_MAX_SUBJECT_CHARS = 120


def _clean_str(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    return text[:limit] if text else None


def _clean_emoji(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    token = value.strip().split(" ")[0] if value.strip() else ""
    # Compound emoji are several code points; cap generously but refuse words.
    if not token or len(token) > 8 or token.isascii():
        return None
    return token


_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_DEFAULT_VOICE_FILE = _TEMPLATES / "voice.default.md"


def _clean(text: str) -> str:
    """Drop HTML comments and surrounding whitespace so a guide can carry notes."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def resolve_voice(inline: Optional[str], file_path: Optional[str]) -> tuple[str, str]:
    """Pick the voice guide: inline var → file → packaged default.

    Returns (voice, source), source in "inline" | "file" | "default", so the
    orchestrator can log which one is in force. Mirrors resolve_criteria.
    """
    if inline and inline.strip():
        return inline.strip(), "inline"
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"NEWSLETTER_VOICE_FILE points at {file_path}, which does not exist"
            )
        text = _clean(path.read_text(encoding="utf-8"))
        if not text:
            raise ValueError(f"NEWSLETTER_VOICE_FILE {file_path} is empty")
        return text, "file"
    return _clean(_DEFAULT_VOICE_FILE.read_text(encoding="utf-8")), "default"


class NewsletterAgent(BaseAgent):
    """Writes the editorial layer of an issue in one call."""

    def __init__(
        self,
        api_key: str,
        model: str,
        provider: str = "google",
        temperature: float = 0.7,
        max_tokens: int = 4000,
        voice: Optional[str] = None,
        voice_file: Optional[str] = None,
    ):
        super().__init__(
            name="NewsletterAgent",
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.voice, self.voice_source = resolve_voice(voice, voice_file)

    @staticmethod
    def payload(issue: NewsletterIssue) -> List[Dict[str, Any]]:
        """The stories the model writes for — everything except placeholders and link_only."""
        rows: List[Dict[str, Any]] = []
        for sec in issue.sections:
            if sec.is_placeholder or sec.spec.style not in _WRITTEN_STYLES:
                continue
            for st in sec.stories:
                rows.append(
                    {
                        "id": st.url_hash,
                        "section": sec.name,
                        "style": sec.spec.style.value if sec.spec.style else None,
                        "source_title": st.source_title,
                        "source": st.source,
                        "category": st.category,
                        "blurb": st.blurb or "",
                        "commentary": bool(
                            sec.spec.style == ItemStyle.FEATURE and sec.spec.commentary
                        ),
                    }
                )
        return rows

    async def compile(
        self, issue: NewsletterIssue, readers: str = "", intro: bool = True
    ) -> NewsletterIssue:
        """Write subject/title/intro and per-story fields. Raises on an unusable reply."""
        rows = self.payload(issue)
        if not rows:
            logger.info(
                "NewsletterAgent: nothing to write (no feature/brief/quick_hit stories)"
            )
            return issue

        prompt = self.load_prompt(
            "newsletter",
            PLATFORM_NAME=issue.platform_name,
            DATE=issue.date_label,
            READERS=readers.strip() or "A general professional audience.",
            VOICE=self.voice,
            INTRO_RULE=(
                "`intro`: the opening, written as the voice guide describes it. Plain text with line breaks; no bullet points."
                if intro
                else "`intro`: this newsletter has no opening. Return an empty string."
            ),
            STORIES_JSON=json.dumps(rows, ensure_ascii=False, indent=1),
        )
        raw = await self._call_llm(user_prompt=prompt, json_output=True)
        data = self._parse_json_response(raw)
        if not isinstance(data, dict):
            raise ValueError("NewsletterAgent: reply is not a JSON object")

        stories = data.get("stories")
        if not isinstance(stories, dict):
            raise ValueError("NewsletterAgent: 'stories' must be an object keyed by id")

        for key, limit in (
            ("subject_line", _MAX_SUBJECT_CHARS),
            ("title", 200),
            ("intro", 1200),
        ):
            val = _clean_str(data.get(key), limit)
            if val:
                setattr(issue, key, val)
        if not intro:
            issue.intro = ""  # structure wins over whatever the model returned

        written, missing = 0, []
        for sec in issue.sections:
            style = sec.spec.style
            if sec.is_placeholder or style not in _WRITTEN_STYLES:
                continue
            for st in sec.stories:
                w = stories.get(st.url_hash)
                if not isinstance(w, dict):
                    missing.append(st.source_title)
                    continue
                headline = _clean_str(w.get("headline"), _MAX_HEADLINE_CHARS)
                if headline:
                    st.headline = headline
                emoji = _clean_emoji(w.get("emoji"))
                if emoji:
                    st.emoji = emoji
                if style == ItemStyle.FEATURE and sec.spec.commentary:
                    commentary = _clean_str(w.get("commentary"), 400)
                    if commentary:
                        st.commentary = commentary
                if style == ItemStyle.BRIEF:
                    anchor = _clean_str(w.get("anchor_text"), _MAX_ANCHOR_CHARS)
                    if anchor:
                        st.anchor_text = anchor
                written += 1

        if missing:
            logger.warning(
                f"NewsletterAgent: model skipped {len(missing)} of {len(rows)} stories; "
                f"they keep source-title fallbacks: "
                + "; ".join(m[:50] for m in missing[:5])
            )
        if written == 0:
            raise ValueError(
                "NewsletterAgent: model wrote none of the stories it was given"
            )

        issue.llm_generated = True
        logger.info(
            f"NewsletterAgent: wrote {written}/{len(rows)} stories, subject '{issue.subject_line[:60]}'"
        )
        return issue
