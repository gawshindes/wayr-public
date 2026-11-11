"""Deduplication Agent - Uses LLM to identify duplicate news stories by canonical event labels."""

import json
import re
from collections import defaultdict
from typing import Any, List, Dict, Optional, Tuple
from wayr.agents.base import BaseAgent
from wayr.utils import clean_summary, get_logger

logger = get_logger(__name__)

# Headlines from different outlets often anchor on different details of one
# announcement; the feed summary is what makes the shared event visible. A
# short excerpt is enough — the prompt scales with the batch, not the article.
SUMMARY_MAX_CHARS = 280

SYSTEM_PROMPT = (
    "You are a conservative news deduplication system. You assign every title a canonical "
    "snake_case event label; titles sharing a label describe the same event. When in doubt, "
    "give titles different labels — false negatives are better than false positives. "
    "Respond only with valid JSON."
)


class DeduplicationAgent(BaseAgent):
    """
    Agent responsible for identifying duplicate news stories using LLM.

    Sends article titles to an LLM which groups them semantically,
    catching duplicates that heuristic approaches would miss.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        provider: str = "google",
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ):
        super().__init__(
            name="DeduplicationAgent",
            provider=provider,
            model=model,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _build_prompt(self, titles: List[Tuple[Any, ...]]) -> str:
        """Build the deduplication prompt from the externalized template.

        Each entry is ``(index, title, label)`` or ``(index, title, label, summary)``;
        a non-empty summary is rendered inline after the title.
        """
        lines = []
        for entry in titles:
            idx, title, label = entry[0], entry[1], entry[2]
            summary = entry[3] if len(entry) > 3 else ""
            if summary:
                lines.append(f'{idx}: "{title}" | summary: "{summary}" [{label}]')
            else:
                lines.append(f'{idx}: "{title}" [{label}]')
        return self.load_prompt("deduplication", TITLES_BLOCK="\n".join(lines))

    def _parse_response(self, response_text: str) -> List[List[int]]:
        """Parse LLM response (event-label schema) into duplicate groups.

        Expected shape:
            {"events": [{"label": "foo_bar", "indices": [0, 5]}, ...]}

        Indices sharing a label form a duplicate group. Groups of size 1 are dropped
        (they represent unique stories with no duplicates).
        """
        buckets: Dict[str, List[int]] = defaultdict(list)

        # First try: standard JSON parsing
        try:
            json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                events = result.get("events", [])
                if isinstance(events, list):
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        label = str(event.get("label", "")).strip() or None
                        indices = event.get("indices", [])
                        if not isinstance(indices, list) or label is None:
                            continue
                        for idx in indices:
                            if isinstance(idx, (int, float)):
                                buckets[label].append(int(idx))
                    return [
                        sorted(set(ids))
                        for ids in buckets.values()
                        if len(set(ids)) >= 2
                    ]
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

        # Fallback: recover complete {"label": "...", "indices": [...]} blocks via regex
        logger.info("JSON malformed/truncated, recovering events via regex...")
        event_pattern = re.compile(
            r'"label"\s*:\s*"([^"]+)"\s*,\s*"indices"\s*:\s*\[([\d,\s]*)\]',
            re.DOTALL,
        )
        for label, indices_str in event_pattern.findall(response_text):
            try:
                indices = [int(x.strip()) for x in indices_str.split(",") if x.strip()]
                if indices:
                    buckets[label].extend(indices)
            except ValueError:
                continue

        validated = [sorted(set(ids)) for ids in buckets.values() if len(set(ids)) >= 2]
        if validated:
            logger.info(
                f"Recovered {len(validated)} duplicate group(s) from partial response"
            )
        elif not buckets:
            logger.warning(f"Could not parse dedup response: {response_text[:200]}")

        return validated

    async def find_duplicates(
        self,
        batch_titles: List[Dict[str, Any]],
        cached_titles: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Find duplicate stories among batch titles and cached titles.

        Each item is ``{"index": int, "title": str}`` with an optional
        ``"summary"`` (the feed description; HTML is stripped here) and an
        optional ``"origin"`` (``"press"`` | ``"brand"`` from the sources file).

        Keeper rules, deliberately different for the two cases:
          - A group with a *cached* member keeps the cached one, whatever the
            origins: that story may already have shipped, and never showing
            an event twice outranks linking the primary source.
          - A group of only batch members keeps a ``brand`` member if there is
            one (the company's own post is the primary source the newsletter
            should link), else the lowest index. Origin is never shown to the
            LLM; it only decides who survives the grouping.
        """
        if not batch_titles:
            return {"duplicate_groups": [], "batch_duplicates_to_remove": set()}

        def _entry(item: Dict[str, Any], label: str) -> Tuple[int, str, str, str]:
            return (
                item["index"],
                item["title"],
                label,
                clean_summary(item.get("summary"), SUMMARY_MAX_CHARS),
            )

        # Build numbered title list
        titles = []
        batch_indices = set()

        for item in batch_titles:
            titles.append(_entry(item, "batch"))
            batch_indices.add(item["index"])

        if cached_titles:
            for item in cached_titles:
                titles.append(_entry(item, "cached"))

        # If only 1 title total, no duplicates possible
        if len(titles) <= 1:
            return {"duplicate_groups": [], "batch_duplicates_to_remove": set()}

        # Build and send prompt
        prompt = self._build_prompt(titles)

        logger.info(
            f"Sending {len(titles)} titles to LLM for deduplication "
            f"({len(batch_titles)} batch + {len(cached_titles or [])} cached)"
        )

        try:
            response_text = await self._call_llm(
                user_prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                json_output=True,
            )
            logger.debug(f"Dedup LLM response: {response_text[:300]}")
        except Exception as e:
            logger.warning(
                f"Dedup LLM call failed: {e}. Skipping LLM dedup (pipeline continues)."
            )
            return {"duplicate_groups": [], "batch_duplicates_to_remove": set()}

        # Parse response
        duplicate_groups = self._parse_response(response_text)

        # Determine which batch articles to remove
        batch_origin = {item["index"]: item.get("origin") for item in batch_titles}
        batch_duplicates_to_remove = set()
        for group in duplicate_groups:
            # Find the "keeper" — prefer cached articles, otherwise keep first batch article
            keeper = None
            for idx in group:
                if idx not in batch_indices:
                    keeper = idx
                    break

            if keeper is not None:
                # Cached article found — remove ALL batch articles in this group
                for idx in group:
                    if idx in batch_indices:
                        batch_duplicates_to_remove.add(idx)
            else:
                # All are batch articles — keep a brand member if any, else the
                # first one; remove the rest
                sorted_group = sorted(
                    (idx for idx in group if idx in batch_indices),
                    key=lambda idx: (0 if batch_origin.get(idx) == "brand" else 1, idx),
                )
                for idx in sorted_group[1:]:
                    batch_duplicates_to_remove.add(idx)

        if duplicate_groups:
            logger.info(
                f"LLM found {len(duplicate_groups)} duplicate group(s), "
                f"removing {len(batch_duplicates_to_remove)} batch article(s)"
            )

            title_map = {entry[0]: entry[1] for entry in titles}
            for i, group in enumerate(duplicate_groups):
                group_titles = [f"  [{idx}] {title_map.get(idx, '?')}" for idx in group]
                logger.info(f"Duplicate group {i+1}:\n" + "\n".join(group_titles))
        else:
            logger.info("LLM found no duplicates")

        return {
            "duplicate_groups": duplicate_groups,
            "batch_duplicates_to_remove": batch_duplicates_to_remove,
        }
