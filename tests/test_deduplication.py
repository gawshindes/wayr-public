"""Tests for the DeduplicationAgent and LLM-based title deduplication."""

import pytest
from unittest.mock import AsyncMock, patch

from wayr.agents.deduplication import DeduplicationAgent


# --- Prompt construction tests ---


class TestPromptConstruction:
    """Test that the dedup prompt is built correctly."""

    def setup_method(self):
        """Create agent with mocked client."""
        with patch(
            "wayr.agents.deduplication.DeduplicationAgent.__init__", return_value=None
        ):
            self.agent = DeduplicationAgent.__new__(DeduplicationAgent)
            self.agent.name = "DeduplicationAgent"
            self.agent.provider = "google"
            self.agent.model = "gemini-flash-latest"
            self.agent.temperature = 0.1
            self.agent.max_tokens = 1024

    def test_prompt_includes_all_titles(self):
        """Prompt should contain all batch and cached titles."""
        titles = [
            (0, "Wootzwork Nets $6.6 Mn To Scale Global OEM Manufacturing", "batch"),
            (1, "Wint Wealth revenue jumps 2.6X in FY25", "batch"),
            (100, "Wootzwork raises $6.6M in Series A led by Z47", "cached"),
        ]
        prompt = self.agent._build_prompt(titles)

        assert "Wootzwork Nets $6.6 Mn" in prompt
        assert "Wint Wealth" in prompt
        assert "Wootzwork raises $6.6M" in prompt
        assert "[batch]" in prompt
        assert "[cached]" in prompt

    def test_prompt_with_empty_cached(self):
        """Prompt should work with only batch titles."""
        titles = [
            (0, "Title A", "batch"),
            (1, "Title B", "batch"),
        ]
        prompt = self.agent._build_prompt(titles)
        assert "0:" in prompt
        assert "1:" in prompt


# --- Response parsing tests ---


class TestResponseParsing:
    """Test parsing of LLM responses."""

    def setup_method(self):
        """Create agent with mocked client."""
        with patch(
            "wayr.agents.deduplication.DeduplicationAgent.__init__", return_value=None
        ):
            self.agent = DeduplicationAgent.__new__(DeduplicationAgent)
            self.agent.name = "DeduplicationAgent"

    def test_parse_valid_response(self):
        """Valid event-label JSON should return correct groups (sorted by label)."""
        response = (
            '{"events": ['
            '{"label": "foo_launches_bar", "indices": [0, 2]},'
            '{"label": "baz_raises_funding", "indices": [3, 5, 7]}'
            "]}"
        )
        result = self.agent._parse_response(response)
        assert sorted(result) == sorted([[0, 2], [3, 5, 7]])

    def test_parse_no_duplicates(self):
        """All-unique labels (singletons) should produce no groups."""
        response = (
            '{"events": ['
            '{"label": "event_a", "indices": [0]},'
            '{"label": "event_b", "indices": [1]}'
            "]}"
        )
        result = self.agent._parse_response(response)
        assert result == []

    def test_parse_invalid_json(self):
        """Invalid JSON should return empty list (graceful fallback)."""
        response = "This is not JSON"
        result = self.agent._parse_response(response)
        assert result == []

    def test_parse_json_with_extra_text(self):
        """JSON wrapped in extra text should still parse."""
        response = 'Here is the result: {"events": [{"label": "x_y_z", "indices": [1, 3]}]} done!'
        result = self.agent._parse_response(response)
        assert result == [[1, 3]]

    def test_parse_single_item_groups_ignored(self):
        """Single-index events (unique stories) are dropped; only 2+ survive."""
        response = (
            '{"events": ['
            '{"label": "solo", "indices": [0]},'
            '{"label": "pair", "indices": [1, 2]}'
            "]}"
        )
        result = self.agent._parse_response(response)
        assert result == [[1, 2]]

    def test_parse_missing_key(self):
        """Missing events key should return empty list."""
        response = '{"other_key": "value"}'
        result = self.agent._parse_response(response)
        assert result == []

    def test_parse_merges_same_label(self):
        """Indices emitted under the same label in separate event entries should merge."""
        response = (
            '{"events": ['
            '{"label": "allbirds_pivots_to_ai", "indices": [2, 5]},'
            '{"label": "allbirds_pivots_to_ai", "indices": [6, 7]}'
            "]}"
        )
        result = self.agent._parse_response(response)
        assert result == [[2, 5, 6, 7]]


# --- Duplicate detection logic tests ---


class TestFindDuplicates:
    """Test the find_duplicates logic (keeper selection, index handling)."""

    def setup_method(self):
        """Create agent with mocked LLM call."""
        with patch(
            "wayr.agents.deduplication.DeduplicationAgent.__init__", return_value=None
        ):
            self.agent = DeduplicationAgent.__new__(DeduplicationAgent)
            self.agent.name = "DeduplicationAgent"
            self.agent.provider = "google"
            self.agent.model = "gemini-flash-latest"
            self.agent.temperature = 0.1
            self.agent.max_tokens = 1024

    @pytest.mark.asyncio
    async def test_batch_only_duplicates(self):
        """Within-batch duplicates: keep first, remove rest."""
        # Mock LLM to return event with indices [0, 2]
        self.agent._call_llm = AsyncMock(
            return_value='{"events": [{"label": "wootzwork_raises_6_6m", "indices": [0, 2]}]}'
        )

        result = await self.agent.find_duplicates(
            batch_titles=[
                {"index": 0, "title": "Wootzwork raises $6.6M in Series A"},
                {"index": 1, "title": "Wint Wealth revenue jumps"},
                {"index": 2, "title": "Wootzwork Nets $6.6 Mn To Scale OEM"},
            ],
        )

        assert result["batch_duplicates_to_remove"] == {2}

    @pytest.mark.asyncio
    async def test_brand_member_is_the_keeper(self):
        """Batch-only group: the brand's own post wins over an earlier press index."""
        self.agent._call_llm = AsyncMock(
            return_value='{"events": [{"label": "astra", "indices": [0, 2]}]}'
        )
        result = await self.agent.find_duplicates(
            batch_titles=[
                {"index": 0, "title": "OpenAI launches GPT-6 Astra", "origin": "press"},
                {"index": 1, "title": "Unrelated"},
                {
                    "index": 2,
                    "title": "GPT-6 Astra: the next generation",
                    "origin": "brand",
                },
            ]
        )
        assert result["batch_duplicates_to_remove"] == {0}

    @pytest.mark.asyncio
    async def test_two_brand_members_keep_lowest_index(self):
        self.agent._call_llm = AsyncMock(
            return_value='{"events": [{"label": "x", "indices": [1, 3]}]}'
        )
        result = await self.agent.find_duplicates(
            batch_titles=[
                {"index": 0, "title": "a"},
                {"index": 1, "title": "b", "origin": "brand"},
                {"index": 2, "title": "c"},
                {"index": 3, "title": "d", "origin": "brand"},
            ]
        )
        assert result["batch_duplicates_to_remove"] == {3}

    @pytest.mark.asyncio
    async def test_cached_member_still_beats_brand(self):
        """A story already in the pool wins regardless of origin — it may have shipped."""
        self.agent._call_llm = AsyncMock(
            return_value='{"events": [{"label": "x", "indices": [0, 100]}]}'
        )
        result = await self.agent.find_duplicates(
            batch_titles=[{"index": 0, "title": "brand post", "origin": "brand"}],
            cached_titles=[{"index": 100, "title": "press coverage from yesterday"}],
        )
        assert result["batch_duplicates_to_remove"] == {0}

    @pytest.mark.asyncio
    async def test_cross_batch_duplicates(self):
        """Cross-batch: cached article present → remove ALL batch dupes in group."""
        # Event [0, 100] where 100 is cached
        self.agent._call_llm = AsyncMock(
            return_value='{"events": [{"label": "wootzwork_raises_6_6m", "indices": [0, 100]}]}'
        )

        result = await self.agent.find_duplicates(
            batch_titles=[
                {"index": 0, "title": "Wootzwork raises $6.6M in Series A"},
                {"index": 1, "title": "Unrelated article"},
            ],
            cached_titles=[
                {"index": 100, "title": "Wootzwork Nets $6.6 Mn To Scale OEM"},
            ],
        )

        assert result["batch_duplicates_to_remove"] == {0}

    @pytest.mark.asyncio
    async def test_llm_failure_graceful(self):
        """LLM failure should return empty results (don't block pipeline)."""
        self.agent._call_llm = AsyncMock(side_effect=Exception("API Error"))

        result = await self.agent.find_duplicates(
            batch_titles=[
                {"index": 0, "title": "Some article"},
                {"index": 1, "title": "Another article"},
            ],
        )

        assert result["batch_duplicates_to_remove"] == set()
        assert result["duplicate_groups"] == []

    @pytest.mark.asyncio
    async def test_no_duplicates(self):
        """No duplicates should return empty results."""
        self.agent._call_llm = AsyncMock(return_value='{"events": []}')

        result = await self.agent.find_duplicates(
            batch_titles=[
                {"index": 0, "title": "Article A"},
                {"index": 1, "title": "Article B"},
            ],
        )

        assert result["batch_duplicates_to_remove"] == set()

    @pytest.mark.asyncio
    async def test_single_article_no_call(self):
        """Single article should skip LLM call entirely."""
        self.agent._call_llm = AsyncMock()

        result = await self.agent.find_duplicates(
            batch_titles=[
                {"index": 0, "title": "Only article"},
            ],
        )

        self.agent._call_llm.assert_not_called()
        assert result["batch_duplicates_to_remove"] == set()

    @pytest.mark.asyncio
    async def test_three_wootzwork_articles(self):
        """The exact Wootzwork scenario: 3 titles about same funding → 2 removed."""
        # Simulate LLM correctly grouping all 3 under one event label
        self.agent._call_llm = AsyncMock(
            return_value='{"events": [{"label": "wootzwork_raises_6_6m", "indices": [0, 1, 2]}]}'
        )

        result = await self.agent.find_duplicates(
            batch_titles=[
                {
                    "index": 0,
                    "title": "Wootzwork Nets $6.6 Mn To Scale Global OEM Manufacturing",
                },
                {
                    "index": 1,
                    "title": "Industrial manufacturing solutions firm Wootzwork raises $6.6 million in round led by Z47",
                },
                {"index": 2, "title": "Wootzwork raises $6.6M in Series A led by Z47"},
            ],
        )

        # Should keep index 0 (first), remove 1 and 2
        assert result["batch_duplicates_to_remove"] == {1, 2}


# --- Summary handling ---


class TestSummaryInPrompt:
    """Feed summaries ride along with titles so the LLM sees the shared event."""

    def setup_method(self):
        with patch(
            "wayr.agents.deduplication.DeduplicationAgent.__init__", return_value=None
        ):
            self.agent = DeduplicationAgent.__new__(DeduplicationAgent)
            self.agent.name = "DeduplicationAgent"
            self.agent.provider = "openai"
            self.agent.model = "gpt-5.4-nano"
            self.agent.temperature = 0.1
            self.agent.max_tokens = 1024

    def test_prompt_renders_summary_inline(self):
        titles = [
            (
                0,
                "Claude comes for Gemini with Docs and Slides",
                "batch",
                "Anthropic is merging chats and Cowork",
            ),
            (1, "Chipotle works with Palantir", "batch", ""),
            (2, "Anthropic merges Claude chat and Cowork", "cached"),
        ]
        prompt = self.agent._build_prompt(titles)
        assert (
            '0: "Claude comes for Gemini with Docs and Slides" | summary: "Anthropic is merging chats and Cowork" [batch]'
            in prompt
        )
        assert '1: "Chipotle works with Palantir" [batch]' in prompt
        assert '2: "Anthropic merges Claude chat and Cowork" [cached]' in prompt
        assert prompt.count('| summary: "') == 1

    @pytest.mark.asyncio
    async def test_find_duplicates_strips_html_and_caps_summary(self):
        from wayr.agents.deduplication import SUMMARY_MAX_CHARS

        self.agent._call_llm = AsyncMock(return_value='{"events": []}')
        long_tail = "x" * (SUMMARY_MAX_CHARS + 50)
        await self.agent.find_duplicates(
            batch_titles=[
                {
                    "index": 0,
                    "title": "A",
                    "summary": "<p>Docs &amp; Slides</p> " + long_tail,
                },
                {"index": 1, "title": "B", "summary": None},
            ],
            cached_titles=[
                {"index": 100, "title": "C", "summary": "<b>cached</b> summary"}
            ],
        )
        prompt = self.agent._call_llm.call_args.kwargs["user_prompt"]
        assert "<p>" not in prompt and "&amp;" not in prompt
        assert 'summary: "Docs & Slides ' in prompt
        assert long_tail not in prompt  # capped
        assert '1: "B" [batch]' in prompt  # missing summary renders title-only
        assert 'summary: "cached summary" [cached]' in prompt

    @pytest.mark.asyncio
    async def test_items_without_summary_key_still_work(self):
        """Old callers and eval cases pass only index + title."""
        self.agent._call_llm = AsyncMock(
            return_value='{"events": [{"label": "same", "indices": [0, 1]}]}'
        )
        result = await self.agent.find_duplicates(
            batch_titles=[{"index": 0, "title": "A"}, {"index": 1, "title": "A again"}]
        )
        assert result["batch_duplicates_to_remove"] == {1}
