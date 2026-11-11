"""Tests for the shared research trunk and the two entry points built on it.

These build a real WAYROrchestrator from a temp env (fake keys, isolated data
dir — agent construction is offline) and mock only discovery and the
per-article classifier, so the factoring itself is what's under test.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from wayr.config import get_settings, reset_settings
from wayr.models import ClassifiedArticle, RSSArticle
from wayr.models.articles import ContentType
from wayr.orchestrator import ResearchResult, WAYROrchestrator

HERMETIC = (
    "PIPELINE_MODE",
    "WORDPRESS_URL",
    "WORDPRESS_USERNAME",
    "WORDPRESS_APP_PASSWORD",
    "BEEHIIV_API_KEY",
    "BEEHIIV_PUBLICATION_ID",
    "ENABLE_DISCOVERY",
    "ENABLE_AUTHOR",
    "ENABLE_CLASSIFICATION",
    # data files: never let a developer's shell point tests at real data/
    "ARTICLE_CACHE_FILE",
    "FEED_LEDGER_FILE",
    "WP_TAXONOMY_CACHE_FILE",
    "NEWSLETTER_RUNS_FILE",
)


def build(tmp_path, monkeypatch, mode="newsletter", **extra) -> WAYROrchestrator:
    for var in HERMETIC:
        monkeypatch.delenv(var, raising=False)
    creds = ""
    if mode in ("newsletter", "both"):
        creds += "BEEHIIV_API_KEY=x\nBEEHIIV_PUBLICATION_ID=pub_x\n"
    if mode in ("news", "both"):
        creds += "WORDPRESS_URL=https://x.example.com\nWORDPRESS_USERNAME=u\nWORDPRESS_APP_PASSWORD=p\n"
    env = tmp_path / "t.env"
    env.write_text(
        f"PIPELINE_MODE={mode}\n{creds}"
        "OPENAI_API_KEY=sk-x\nGOOGLE_API_KEY=g-x\nINDUSTRY=Technology\nINDUSTRY_CATEGORIES=AI\n"
        f"ARTICLE_CACHE_FILE={tmp_path}/processed.json\nFEED_LEDGER_FILE={tmp_path}/ledger.json\n"
        f"WP_TAXONOMY_CACHE_FILE={tmp_path}/wp.json\nNEWSLETTER_RUNS_FILE={tmp_path}/runs.json\nLOG_LEVEL=WARNING\n"
        + "".join(f"{k}={v}\n" for k, v in extra.items())
    )
    reset_settings()
    get_settings(env_file=str(env))
    o = WAYROrchestrator()
    # Offline by default: the newsletter path calls two LLM agents and a
    # publisher. Tests that exercise one of them override the stub.
    if o.summary_agent is not None:
        o.summary_agent.generate_summary_from_source = AsyncMock(return_value="")
    if o.newsletter_agent is not None:
        o.newsletter_agent.compile = AsyncMock(
            side_effect=lambda issue, readers="", intro=True: issue
        )
    if o.dedup_agent is not None:
        o.dedup_agent.find_duplicates = AsyncMock(
            return_value={"duplicate_groups": [], "batch_duplicates_to_remove": set()}
        )
    if o.newsletter_publisher is not None:
        from wayr.publishers.base import PublishError

        o.newsletter_publisher.publish = AsyncMock(
            side_effect=PublishError("publisher not stubbed in this test")
        )
    return o


def art(n):
    return RSSArticle(
        title=f"Story {n}",
        url=f"https://example.com/{n}",
        source="Ex",
        published_date=datetime.now(
            timezone.utc
        ),  # fresh; freshness tests set their own
        content="w " * 60,
    )


def classify(article):
    """Story 2 is a review; everything else is news."""
    is_news = "2" not in article.title
    return ClassifiedArticle(
        original=article,
        belongs_to_industry=True,
        content_type=ContentType.NEWS if is_news else ContentType.REVIEW,
        content_type_reason="test",
        categories=["AI"] if is_news else [],
        classification_reason="test",
    )


def as_news(article):
    """Explicit news classification for seeding backlogs (independent of classify())."""
    return ClassifiedArticle(
        original=article,
        belongs_to_industry=True,
        content_type=ContentType.NEWS,
        content_type_reason="seed",
        categories=["AI"],
        classification_reason="seed",
    )


def wire(o, articles):
    o.discovery_agent.discover_articles = AsyncMock(return_value=articles)
    o._classify_article = AsyncMock(side_effect=classify)


@pytest.fixture(autouse=True)
def _reset():
    yield
    reset_settings()


class TestResearch:
    @pytest.mark.asyncio
    async def test_returns_all_outcomes_and_advances_step(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        wire(o, [art(1), art(2), art(3)])
        result, step = await o._research(step=0, total_steps=2)
        assert step == 2
        assert len(result.discovered) == 3
        assert len(result.classified) == 3  # non-news included
        assert [c.original.title for c in result.news] == ["Story 1", "Story 3"]
        assert result.non_news_counts == {"review": 1}

    @pytest.mark.asyncio
    async def test_persists_classification(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        wire(o, [art(1), art(2)])
        await o._research(step=0, total_steps=2)
        stages = sorted(e["last_stage"] for e in o.article_tracker.processed.values())
        assert stages == ["category", "news"]  # news → category; review stops at news

    @pytest.mark.asyncio
    async def test_classifier_exception_is_captured_not_raised(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch)
        o.discovery_agent.discover_articles = AsyncMock(return_value=[art(1), art(2)])
        o._classify_article = AsyncMock(
            side_effect=[classify(art(1)), RuntimeError("llm down")]
        )
        result, _ = await o._research(step=0, total_steps=2)
        assert len(result.classified) == 1
        assert result.classification_errors == [
            {"title": "Story 2", "error": "llm down"}
        ]

    @pytest.mark.asyncio
    async def test_discovery_disabled_is_a_noop(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, ENABLE_DISCOVERY="false")
        assert o.discovery_agent is None
        result, step = await o._research(step=0, total_steps=1)
        assert result == ResearchResult() and step == 0


class TestRunPrework:
    @pytest.mark.asyncio
    async def test_researches_and_reports_without_authoring(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch)
        wire(o, [art(1), art(2), art(3)])
        results = await o.run_prework()
        assert len(results["discovered"]) == 3
        assert len(results["classified"]) == 3
        assert [c.original.title for c in results["news"]] == ["Story 1", "Story 3"]
        assert results["errors"] == []
        assert set(results["ledger"]) >= {"feeds", "items", "admitted", "unadmitted"}
        assert o.author_agent is None and o.publishing_agent is None

    @pytest.mark.asyncio
    async def test_no_candidate_recovery_and_no_publish_slot_gate(
        self, tmp_path, monkeypatch
    ):
        """The whole point of prework: a backlog must never suppress discovery."""
        o = build(tmp_path, monkeypatch)
        # Seed a backlog bigger than any publish cap.
        backlog = [as_news(art(100 + i)) for i in range(10)]
        o.article_tracker.record_classification(backlog)
        assert len(o.article_tracker.get_news_candidates()) == 10

        wire(o, [art(1)])
        results = await o.run_prework()
        o.discovery_agent.discover_articles.assert_awaited_once()  # not skipped
        assert [c.original.title for c in results["news"]] == [
            "Story 1"
        ]  # backlog not merged in

    @pytest.mark.asyncio
    async def test_discovery_disabled(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, ENABLE_DISCOVERY="false")
        results = await o.run_prework()
        assert results["discovered"] == [] and results["errors"] == []


class TestRunPipelineStillWorks:
    """News mode goes through the same trunk and behaves as before."""

    @pytest.mark.asyncio
    async def test_reaches_author_gate_with_research_results(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch, mode="news", ENABLE_AUTHOR="false")
        wire(o, [art(1), art(2), art(3)])
        results = await o.run_pipeline(skip_publishing=True)
        assert len(results["discovered"]) == 3
        assert len(results["classified"]) == 3
        assert results["generated"] == [] and results["published"] == []
        assert results["errors"] == []

    @pytest.mark.asyncio
    async def test_publish_slot_gate_still_skips_discovery_in_news_mode(
        self, tmp_path, monkeypatch
    ):
        """Contrast with prework: the per-story pipeline keeps its backpressure."""
        o = build(
            tmp_path,
            monkeypatch,
            mode="news",
            ENABLE_AUTHOR="false",
            MAX_ARTICLES_PUBLISHED_PER_RUN="2",
        )
        backlog = [as_news(art(100 + i)) for i in range(3)]
        o.article_tracker.record_classification(backlog)
        o._reconstruct_candidates = AsyncMock(return_value=backlog)
        wire(o, [art(1)])
        await o.run_pipeline(skip_publishing=True)
        o.discovery_agent.discover_articles.assert_not_awaited()


class TestPreworkImportance:
    """Importance runs after research, on news only, and is persisted."""

    @pytest.mark.asyncio
    async def test_scores_news_only_and_persists(self, tmp_path, monkeypatch):
        from wayr.models import ImportanceResult

        o = build(tmp_path, monkeypatch)
        assert o.importance_agent is not None  # newsletter mode builds it
        wire(o, [art(1), art(2), art(3)])  # Story 2 is a review
        o.importance_agent.score = AsyncMock(
            side_effect=lambda c: ImportanceResult(
                score=0.9 if "1" in c.original.title else 0.4, reasoning="r"
            )
        )

        results = await o.run_prework()

        assert o.importance_agent.score.await_count == 2  # not the review
        assert {c.original.title: c.importance_score for c in results["scored"]} == {
            "Story 1": 0.9,
            "Story 3": 0.4,
        }
        entries = {e["title"]: e for e in o.article_tracker.processed.values()}
        assert entries["Story 1"]["importance"]["score"] == 0.9
        assert entries["Story 1"]["last_stage"] == "category"  # not advanced
        assert "importance" not in entries["Story 2"]
        assert entries["Story 1"]["eligible_at"] == entries["Story 1"]["created_at"]

    @pytest.mark.asyncio
    async def test_one_scoring_failure_does_not_lose_the_rest(
        self, tmp_path, monkeypatch
    ):
        from wayr.models import ImportanceResult

        o = build(tmp_path, monkeypatch)
        wire(o, [art(1), art(3)])
        o.importance_agent.score = AsyncMock(
            side_effect=[
                ImportanceResult(score=0.7, reasoning="r"),
                RuntimeError("llm down"),
            ]
        )
        results = await o.run_prework()
        assert [c.original.title for c in results["scored"]] == ["Story 1"]
        assert results["importance_errors"] == [
            {"title": "Story 3", "error": "llm down"}
        ]
        assert results["errors"] == []

    def test_news_mode_does_not_build_importance(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, mode="news")
        assert o.importance_agent is None


class TestRunNewsletterSelection:
    """run_newsletter up to selection: window, guard, candidates, tiers."""

    @staticmethod
    def _pool(o, n, score_fn):

        arts = [as_news(art(i)) for i in range(n)]
        o.article_tracker.record_classification(arts)
        for a in arts:
            r = score_fn(a)
            a.importance_score, a.importance_reason = r, "r"
        o.article_tracker.record_importance(arts)

    @pytest.mark.asyncio
    async def test_bootstrap_window_selects_tiers(self, tmp_path, monkeypatch):
        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MIN_STORIES="1",
            NEWSLETTER_MAX_STORIES="10",
        )
        self._pool(
            o,
            4,
            lambda a: {"Story 0": 0.9, "Story 1": 0.7, "Story 2": 0.4, "Story 3": 0.1}[
                a.original.title
            ],
        )
        r = await o.run_newsletter(skip_publishing=True)
        assert r["status"] == "preview" and r["window_source"] == "bootstrap"
        assert [d["title"] for d in r["featured"]] == ["Story 0", "Story 1"]
        assert [d["title"] for d in r["roundup"]] == ["Story 2"]
        assert [d["title"] for d in r["dropped"]] == ["Story 3"]
        assert o.run_log.runs == []  # nothing published → nothing recorded

    @pytest.mark.asyncio
    async def test_empty_window(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        r = await o.run_newsletter(skip_publishing=True)
        assert r["status"] == "empty" and r["candidates"] == []

    @pytest.mark.asyncio
    async def test_same_day_guard_and_force(self, tmp_path, monkeypatch):
        from wayr.services import NewsletterRun

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        self._pool(o, 1, lambda a: 0.9)
        now = datetime.now(timezone.utc)
        o.run_log.record(
            NewsletterRun(
                window_start=(now - timedelta(hours=24)).isoformat(),
                window_end=(now - timedelta(minutes=5)).isoformat(),
                status="success",
                story_count=3,
            )
        )
        assert (await o.run_newsletter(skip_publishing=True))[
            "status"
        ] == "already_sent"
        assert (await o.run_newsletter(force=True, skip_publishing=True))["status"] in (
            "preview",
            "empty",
        )

    @pytest.mark.asyncio
    async def test_window_override_skips_guard_and_watermark(
        self, tmp_path, monkeypatch
    ):
        from wayr.services import NewsletterRun

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        self._pool(o, 1, lambda a: 0.9)
        now = datetime.now(timezone.utc)
        o.run_log.record(
            NewsletterRun(
                window_start=now.isoformat(),
                window_end=now.isoformat(),
                status="success",
            )
        )
        r = await o.run_newsletter(window_hours=48, skip_publishing=True)
        assert r["status"] == "preview" and r["window_source"] == "override"
        assert [d["title"] for d in r["featured"]] == ["Story 0"]

    @pytest.mark.asyncio
    async def test_stale_story_excluded_by_freshness_even_if_in_window(
        self, tmp_path, monkeypatch
    ):
        """Cold start: still in the feed, published five days ago → not today's news."""
        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MIN_STORIES="1",
            NEWSLETTER_MAX_STORY_AGE_HOURS="36",
        )
        fresh = as_news(art(1))
        stale = as_news(art(2))
        stale.original.published_date = datetime.now(timezone.utc) - timedelta(days=5)
        o.article_tracker.record_classification([fresh, stale])
        for a in (fresh, stale):
            a.importance_score = 0.9
        o.article_tracker.record_importance([fresh, stale])
        r = await o.run_newsletter(skip_publishing=True)
        assert [d["title"] for d in r["candidates"]] == ["Story 1"]


class TestRunNewsletterStructure:
    @pytest.mark.asyncio
    async def test_selection_is_assigned_and_rendered(self, tmp_path, monkeypatch):
        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MIN_STORIES="1",
            NEWSLETTER_MAX_STORIES="10",
        )
        TestRunNewsletterSelection._pool(
            o,
            3,
            lambda a: {"Story 0": 0.9, "Story 1": 0.7, "Story 2": 0.4}[
                a.original.title
            ],
        )
        r = await o.run_newsletter(skip_publishing=True)
        assert r["status"] == "preview"
        names = [sec["name"] for sec in r["sections"]]
        assert names == ["Top Story", "What happened today", "Also worth knowing"]
        assert [sec["stories"] for sec in r["sections"]] == [1, 1, 1]
        assert r["unplaced"] == []
        assert r["issue"].llm_generated is False and r["issue"].story_count == 3
        html = r["html"]
        assert "Story 0" in html and "Story 2" in html and "<h2>Top Story</h2>" in html

    def test_news_mode_has_no_structure_agents(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, mode="news")
        assert o.section_schema is None and o.renderer is None

    def test_bad_schema_fails_at_startup(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.yaml"
        bad.write_text("sections:\n  - {name: X, type: stories}\n")
        with pytest.raises(Exception, match="need a style"):
            build(tmp_path, monkeypatch, NEWSLETTER_SECTIONS_FILE=str(bad))


class TestRunNewsletterCompile:
    """Blurbs from persisted content, compile call, and the fallback contract."""

    @staticmethod
    async def _run(o, compile_reply=None, compile_error=None):
        from wayr.models.newsletter import NewsletterIssue

        o.summary_agent.generate_summary_from_source = AsyncMock(
            side_effect=lambda title, content: (
                f"blurb({title}|{content[:8]})" if content else ""
            )
        )
        if compile_error:
            o.newsletter_agent.compile = AsyncMock(side_effect=compile_error)
        else:

            async def fake_compile(issue: NewsletterIssue, readers="", intro=True):
                issue.subject_line = "COMPILED SUBJECT"
                for sec in issue.sections:
                    for st in sec.stories:
                        st.headline = f"H:{st.source_title}"
                issue.llm_generated = True
                return issue

            o.newsletter_agent.compile = AsyncMock(side_effect=fake_compile)
        return await o.run_newsletter(skip_publishing=True)

    @pytest.mark.asyncio
    async def test_blurbs_from_tracker_content_then_compile(
        self, tmp_path, monkeypatch
    ):
        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MIN_STORIES="1",
            NEWSLETTER_MAX_STORIES="10",
        )
        assert o.newsletter_agent is not None
        TestRunNewsletterSelection._pool(o, 2, lambda a: 0.9)
        # prework persisted content on the news-stage entries
        assert all(e.get("content") for e in o.article_tracker.processed.values())
        r = await self._run(o)
        assert (
            r["compile"]["llm_generated"] is True
            and r["compile"]["compile_error"] is None
        )
        assert r["compile"]["summaries"] == 2 and r["compile"]["summary_errors"] == []
        assert r["issue"].subject_line == "COMPILED SUBJECT"
        assert "H:Story 0" in r["html"] and "blurb(Story 0|w w w w " in r["html"]

    @pytest.mark.asyncio
    async def test_compile_failure_ships_deterministic_issue(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        TestRunNewsletterSelection._pool(o, 1, lambda a: 0.9)
        r = await self._run(o, compile_error=RuntimeError("model down"))
        assert r["status"] == "preview"
        assert (
            r["compile"]["llm_generated"] is False
            and "model down" in r["compile"]["compile_error"]
        )
        assert "Daily Brief" in r["issue"].title and "Story 0" in r["html"]
        assert r["errors"] == []  # not a run failure

    @pytest.mark.asyncio
    async def test_link_only_stories_are_not_summarized(self, tmp_path, monkeypatch):
        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MIN_STORIES="1",
            NEWSLETTER_MAX_STORIES="1",
        )
        # two stories: one featured (needs blurb), one demoted to roundup → link_only section
        TestRunNewsletterSelection._pool(o, 2, lambda a: 0.9)
        r = await self._run(o)
        assert r["counts"]["featured"] == 1 and r["counts"]["roundup"] == 1
        assert o.summary_agent.generate_summary_from_source.await_count == 1

    @pytest.mark.asyncio
    async def test_content_chain_falls_back_to_ledger_then_extract(
        self, tmp_path, monkeypatch
    ):
        from wayr.models.newsletter import NewsletterStory, Tier

        o = build(tmp_path, monkeypatch)
        st = NewsletterStory(
            url_hash="h1",
            url="https://ex.com/1",
            source="S",
            source_title="T",
            tier=Tier.FEATURE,
        )
        # 1. nothing anywhere → extract is tried
        with patch("wayr.orchestrator.ContentExtractor") as CE:
            CE.return_value.extract = AsyncMock(return_value="extracted body")
            assert await o._content_for(st) == "extracted body"
        # 2. ledger rss_summary beats extraction
        o.feed_ledger.items["h1"] = {"rss_summary": "from the feed"}
        assert await o._content_for(st) == "from the feed"
        # 3. tracker content beats the ledger
        o.article_tracker.processed["h1"] = {
            "last_stage": "category",
            "content": "from tracker",
        }
        assert await o._content_for(st) == "from tracker"

    @pytest.mark.asyncio
    async def test_extract_failure_yields_empty_not_exception(
        self, tmp_path, monkeypatch
    ):
        from wayr.models.newsletter import NewsletterStory, Tier

        o = build(tmp_path, monkeypatch)
        st = NewsletterStory(
            url_hash="h2",
            url="https://ex.com/2",
            source="S",
            source_title="T",
            tier=Tier.FEATURE,
        )
        with patch("wayr.orchestrator.ContentExtractor") as CE:
            CE.return_value.extract = AsyncMock(side_effect=RuntimeError("403"))
            assert await o._content_for(st) == ""


class TestRunNewsletterPublish:
    """The watermark advances only on a successful publish."""

    @staticmethod
    def _prep(o):
        from wayr.models.newsletter import NewsletterIssue

        TestRunNewsletterSelection._pool(o, 1, lambda a: 0.9)
        o.summary_agent.generate_summary_from_source = AsyncMock(return_value="b")

        async def fake_compile(issue: NewsletterIssue, readers=""):
            issue.llm_generated = True
            return issue

        o.newsletter_agent.compile = AsyncMock(side_effect=fake_compile)

    @pytest.mark.asyncio
    async def test_success_records_run_and_advances_watermark(
        self, tmp_path, monkeypatch
    ):
        from wayr.publishers.base import PublishResult

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        assert (
            o.newsletter_publisher is not None
            and o.newsletter_publisher.name == "beehiiv"
        )
        self._prep(o)
        o.newsletter_publisher.publish = AsyncMock(
            return_value=PublishResult(
                publisher="beehiiv",
                external_id="post_1",
                url="https://b/p/1",
                status="draft",
            )
        )
        assert o.run_log.last_successful_window_end() is None
        r = await o.run_newsletter()
        assert (
            r["status"] == "published"
            and r["external_id"] == "post_1"
            and r["draft_url"] == "https://b/p/1"
        )
        assert o.run_log.last_successful_window_end().isoformat() == r["window_end"]
        assert (
            o.run_log.runs[-1]["status"] == "success"
            and o.run_log.runs[-1]["story_count"] == 1
        )
        issue, html = o.newsletter_publisher.publish.await_args.args
        assert issue.story_count == 1 and "Story 0" in html

    @pytest.mark.asyncio
    async def test_failure_records_failed_run_and_keeps_watermark(
        self, tmp_path, monkeypatch
    ):
        from wayr.publishers.base import PublishError

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        self._prep(o)
        o.newsletter_publisher.publish = AsyncMock(
            side_effect=PublishError("beehiiv 500")
        )
        r = await o.run_newsletter()
        assert r["status"] == "failed" and "beehiiv 500" in r["errors"][0]
        assert (
            o.run_log.runs[-1]["status"] == "failed"
            and "beehiiv 500" in o.run_log.runs[-1]["error"]
        )
        assert o.run_log.last_successful_window_end() is None  # not advanced
        # the next run re-reads the same window and is not blocked by the guard
        o.newsletter_publisher.publish = AsyncMock(
            side_effect=PublishError("still down")
        )
        assert (await o.run_newsletter())["status"] == "failed"

    @pytest.mark.asyncio
    async def test_preview_never_records(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        self._prep(o)
        o.newsletter_publisher.publish = AsyncMock()
        r = await o.run_newsletter(skip_publishing=True)
        assert r["status"] == "preview" and r["html"]
        o.newsletter_publisher.publish.assert_not_awaited()
        assert o.run_log.runs == []

    @pytest.mark.asyncio
    async def test_same_day_guard_after_a_real_success(self, tmp_path, monkeypatch):
        from wayr.publishers.base import PublishResult

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        self._prep(o)
        o.newsletter_publisher.publish = AsyncMock(
            return_value=PublishResult(
                publisher="beehiiv", external_id="p", url=None, status="accepted"
            )
        )
        assert (await o.run_newsletter())["status"] == "published"
        assert (await o.run_newsletter())["status"] == "already_sent"
        assert o.newsletter_publisher.publish.await_count == 1

    def test_news_mode_has_no_newsletter_publisher(self, tmp_path, monkeypatch):
        assert build(tmp_path, monkeypatch, mode="news").newsletter_publisher is None


class TestDualMode:
    """PIPELINE_MODE=both: one research pass feeds both the site and the pool."""

    def test_builds_news_and_newsletter_agents(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, mode="both")
        assert o.author_agent and o.publishing_agent  # news branch
        assert (
            o.importance_agent and o.newsletter_agent and o.newsletter_publisher
        )  # newsletter
        assert o.section_schema and o.renderer

    @pytest.mark.asyncio
    async def test_pipeline_scores_the_pool_after_research(self, tmp_path, monkeypatch):
        from wayr.models import ImportanceResult

        o = build(tmp_path, monkeypatch, mode="both", ENABLE_AUTHOR="false")
        wire(o, [art(1), art(2), art(3)])  # Story 2 is a review
        o.importance_agent.score = AsyncMock(
            return_value=ImportanceResult(score=0.8, reasoning="r")
        )
        r = await o.run_pipeline(skip_publishing=True)
        assert o.importance_agent.score.await_count == 2  # news only
        assert len(r["scored"]) == 2
        pooled = {e["title"]: e for e in o.article_tracker.processed.values()}
        assert pooled["Story 1"]["importance"]["score"] == 0.8
        assert "importance" not in pooled["Story 2"]

    @pytest.mark.asyncio
    async def test_backlog_never_skips_discovery_in_dual_mode(
        self, tmp_path, monkeypatch
    ):
        """Contrast with news mode, where the publish-slot gate skips discovery."""
        o = build(
            tmp_path,
            monkeypatch,
            mode="both",
            ENABLE_AUTHOR="false",
            MAX_ARTICLES_PUBLISHED_PER_RUN="2",
        )
        backlog = [as_news(art(100 + i)) for i in range(3)]
        o.article_tracker.record_classification(backlog)
        o._reconstruct_candidates = AsyncMock(return_value=backlog)
        wire(o, [art(1)])
        o.importance_agent.score = AsyncMock(
            side_effect=lambda c: __import__(
                "wayr.models", fromlist=["ImportanceResult"]
            ).ImportanceResult(score=0.5, reasoning="r")
        )
        await o.run_pipeline(skip_publishing=True)
        o.discovery_agent.discover_articles.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_newsletter_window_includes_stories_the_site_published(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch, mode="both", NEWSLETTER_MIN_STORIES="1")
        a = as_news(art(1))
        o.article_tracker.record_classification([a])
        a.importance_score, a.importance_reason = 0.9, "r"
        o.article_tracker.record_importance([a])
        o.article_tracker.record_published([a.original])  # the site ran it
        assert (
            o.article_tracker.processed[list(o.article_tracker.processed)[0]][
                "last_stage"
            ]
            == "published"
        )
        r = await o.run_newsletter(skip_publishing=True)
        assert [d["title"] for d in r["featured"]] == ["Story 1"]

    def test_news_mode_window_still_excludes_nothing_extra(self, tmp_path, monkeypatch):
        """Published stories were never in the window before; now they are — in every mode.
        In newsletter mode nothing reaches 'published', so behaviour there is unchanged.
        """
        o = build(tmp_path, monkeypatch)
        assert all(
            e["last_stage"] != "published" for e in o.article_tracker.processed.values()
        )


class TestNewsletterOverNewsModePool:
    """Scenario A: the pool was filled by a news-mode pipeline — no importance scores,
    some stories already published, entries predating the newsletter fields."""

    @pytest.mark.asyncio
    async def test_unscored_candidates_are_scored_persisted_and_selected(
        self, tmp_path, monkeypatch
    ):
        from wayr.models import ImportanceResult

        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MIN_STORIES="1",
            NEWSLETTER_MAX_STORIES="10",
        )
        pool = [as_news(art(i)) for i in range(3)]
        o.article_tracker.record_classification(
            pool
        )  # no record_importance — as news mode leaves it
        o.article_tracker.record_published([pool[0].original])  # the site ran one
        o.importance_agent.score = AsyncMock(
            side_effect=lambda c: ImportanceResult(
                score={"Story 0": 0.9, "Story 1": 0.7, "Story 2": 0.2}[
                    c.original.title
                ],
                reasoning="r",
            )
        )

        r = await o.run_newsletter(skip_publishing=True, window_hours=48)

        assert o.importance_agent.score.await_count == 3
        assert r["lazy_scored"] == 3 and r["importance_errors"] == []
        assert [d["title"] for d in r["featured"]] == [
            "Story 0",
            "Story 1",
        ]  # published story included
        assert [d["title"] for d in r["dropped"]] == ["Story 2"]
        assert r["unscored"] == []
        pooled = {e["title"]: e for e in o.article_tracker.processed.values()}
        assert pooled["Story 0"]["importance"]["score"] == 0.9  # persisted
        assert pooled["Story 0"]["last_stage"] == "published"  # stage untouched

    @pytest.mark.asyncio
    async def test_already_scored_candidates_are_not_rescored(
        self, tmp_path, monkeypatch
    ):
        from wayr.models import ImportanceResult

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        TestRunNewsletterSelection._pool(o, 2, lambda a: 0.8)  # scored
        extra = as_news(art(9))
        o.article_tracker.record_classification([extra])  # unscored
        o.importance_agent.score = AsyncMock(
            return_value=ImportanceResult(score=0.5, reasoning="r")
        )
        r = await o.run_newsletter(skip_publishing=True, window_hours=48)
        assert o.importance_agent.score.await_count == 1
        assert r["lazy_scored"] == 1

    @pytest.mark.asyncio
    async def test_legacy_entry_without_newsletter_fields_still_selects(
        self, tmp_path, monkeypatch
    ):
        """An entry written before eligible_at/rss_published_at/content existed."""
        from wayr.models import ImportanceResult

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        now = datetime.now(timezone.utc).isoformat()
        o.article_tracker.processed["legacyhash"] = {
            "created_at": now,
            "updated_at": now,
            "title": "Legacy story",
            "url": "https://ex.com/legacy",
            "source": "Ex",
            "last_stage": "category",
            "industry": {"belongs": True},
            "news_type": {"content_type": "news"},
            "category": {"primary": "AI", "secondary": "", "reasoning": ""},
        }
        o.importance_agent.score = AsyncMock(
            return_value=ImportanceResult(score=0.8, reasoning="r")
        )
        with patch("wayr.orchestrator.ContentExtractor") as CE:
            CE.return_value.extract = AsyncMock(return_value="re-extracted body")
            r = await o.run_newsletter(skip_publishing=True, window_hours=48)
        assert [d["title"] for d in r["featured"]] == ["Legacy story"]
        CE.return_value.extract.assert_awaited()  # no tracker content → re-extract
        scored_input = o.importance_agent.score.await_args.args[0]
        assert (
            scored_input.original.content == "re-extracted body"
            and scored_input.category == "AI"
        )

    @pytest.mark.asyncio
    async def test_one_scoring_failure_does_not_block_selection(
        self, tmp_path, monkeypatch
    ):
        from wayr.models import ImportanceResult

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        o.article_tracker.record_classification([as_news(art(1)), as_news(art(2))])
        o.importance_agent.score = AsyncMock(
            side_effect=[
                ImportanceResult(score=0.9, reasoning="r"),
                RuntimeError("llm down"),
            ]
        )
        r = await o.run_newsletter(skip_publishing=True, window_hours=48)
        assert r["lazy_scored"] == 1 and len(r["importance_errors"]) == 1
        assert [d["title"] for d in r["featured"]] == ["Story 1"]
        assert [d["title"] for d in r["unscored"]] == ["Story 2"]  # visible, not lost


class TestWordPressNewsletterPublisher:
    def test_newsletter_mode_can_use_wordpress_drafts(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, mode="both", NEWSLETTER_PUBLISHER="wordpress")
        assert (
            o.newsletter_publisher is not None
            and o.newsletter_publisher.name == "wordpress"
        )

    @pytest.mark.asyncio
    async def test_draft_flows_through_run_newsletter(self, tmp_path, monkeypatch):
        from wayr.publishers.base import PublishResult

        o = build(
            tmp_path,
            monkeypatch,
            mode="both",
            NEWSLETTER_PUBLISHER="wordpress",
            NEWSLETTER_MIN_STORIES="1",
        )
        TestRunNewsletterPublish._prep(o)
        o.newsletter_publisher.publish = AsyncMock(
            return_value=PublishResult(
                publisher="wordpress",
                external_id="77",
                url="https://cms/?p=77",
                status="draft",
            )
        )
        r = await o.run_newsletter()
        assert (
            r["status"] == "published"
            and r["publisher"] == "wordpress"
            and r["external_id"] == "77"
        )
        assert o.run_log.runs[-1]["publisher"] == "wordpress"


class TestNeverSendAStoryTwice:
    @pytest.mark.asyncio
    async def test_overlapping_window_excludes_shipped_stories(
        self, tmp_path, monkeypatch
    ):
        from wayr.publishers.base import PublishResult

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        TestRunNewsletterPublish._prep(o)  # one scored story in the pool
        o.newsletter_publisher.publish = AsyncMock(
            return_value=PublishResult(
                publisher="beehiiv", external_id="p1", url=None, status="draft"
            )
        )
        first = await o.run_newsletter(
            window_hours=48
        )  # override never advances the watermark
        assert first["status"] == "published" and first["already_sent"] == 0
        assert len(o.run_log.runs[-1]["story_hashes"]) == 1

        again = await o.run_newsletter(window_hours=48)  # same overlapping window
        assert again["status"] == "empty" and again["already_sent"] == 1
        assert o.newsletter_publisher.publish.await_count == 1

        inspect = await o.run_newsletter(
            window_hours=48, include_sent=True, skip_publishing=True
        )
        assert inspect["status"] == "preview" and inspect["already_sent"] == 0

    @pytest.mark.asyncio
    async def test_failed_push_does_not_mark_stories_sent(self, tmp_path, monkeypatch):
        from wayr.publishers.base import PublishError

        o = build(tmp_path, monkeypatch, NEWSLETTER_MIN_STORIES="1")
        TestRunNewsletterPublish._prep(o)
        o.newsletter_publisher.publish = AsyncMock(side_effect=PublishError("403"))
        assert (await o.run_newsletter(window_hours=48))["status"] == "failed"
        assert o.run_log.sent_hashes() == set()
        retry = await o.run_newsletter(window_hours=48, skip_publishing=True)
        assert retry["already_sent"] == 0 and len(retry["candidates"]) == 1

    @pytest.mark.asyncio
    async def test_window_override_lifts_the_freshness_cap(self, tmp_path, monkeypatch):
        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MIN_STORIES="1",
            NEWSLETTER_MAX_STORY_AGE_HOURS="36",
        )
        old = as_news(art(7))
        old.original.published_date = datetime.now(timezone.utc) - timedelta(hours=60)
        o.article_tracker.record_classification([old])
        old.importance_score = 0.9
        o.article_tracker.record_importance([old])
        capped = await o.run_newsletter(skip_publishing=True)  # default window, 36h cap
        assert capped["status"] == "empty"
        widened = await o.run_newsletter(skip_publishing=True, window_hours=96)
        assert [d["title"] for d in widened["candidates"]] == ["Story 7"]


class TestRunMirrorStripsHashes:
    def test_story_hashes_not_sent_to_supabase(self):
        from unittest.mock import MagicMock
        from wayr.services import supabase_store as ss

        st = ss.SupabasePipelineStore.__new__(ss.SupabasePipelineStore)
        st.client, st.region = MagicMock(), "us"
        st.record_newsletter_run(
            {"id": 9, "status": "success", "story_hashes": ["a"], "story_count": 1}
        )
        row = st.client.table.return_value.insert.call_args.args[0]
        assert "story_hashes" not in row and "id" not in row and row["region"] == "us"


class TestPlanNewsletter:
    """The preflight: pure reads that let the product ask instead of the engine deciding."""

    @staticmethod
    def _story(o, n, hours_old, score=0.8):
        a = as_news(art(n))
        a.original.published_date = datetime.now(timezone.utc) - timedelta(
            hours=hours_old
        )
        o.article_tracker.record_classification([a])
        a.importance_score, a.importance_reason = score, "r"
        o.article_tracker.record_importance([a])
        h = next(
            k
            for k, e in o.article_tracker.processed.items()
            if e["title"] == f"Story {n}"
        )
        when = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
        o.article_tracker.processed[h]["eligible_at"] = when
        o.article_tracker.processed[h]["created_at"] = when
        return h

    @staticmethod
    def _success(o, hours_ago, hashes=()):
        from wayr.services import NewsletterRun

        when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        o.run_log.record(
            NewsletterRun(
                window_start=(when - timedelta(hours=24)).isoformat(),
                window_end=when.isoformat(),
                status="success",
                story_hashes=list(hashes),
                created_at=when.isoformat(),
            )
        )

    def test_no_history(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, NEWSLETTER_MAX_STORY_AGE_HOURS="36")
        self._story(o, 1, hours_old=5)
        p = o.plan_newsletter()
        assert p["last_successful_at"] is None and p["hours_since_last"] is None
        assert p["missed_schedule"] is False and p["since_last_issue"] is None
        assert p["usual"]["candidates"] == 1 and p["usual"]["window_hours"] == 36

    def test_on_schedule_is_not_a_miss(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, NEWSLETTER_MAX_STORY_AGE_HOURS="36")
        self._story(o, 1, hours_old=5)
        self._success(o, hours_ago=24)
        p = o.plan_newsletter()
        assert p["missed_schedule"] is False and 23.5 < p["hours_since_last"] < 24.5
        assert p["since_last_issue"]["candidates"] == 1

    def test_missed_schedule_shows_what_each_choice_yields(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, NEWSLETTER_MAX_STORY_AGE_HOURS="36")
        self._story(o, 1, hours_old=5)  # within the usual window
        self._story(o, 3, hours_old=60)  # only reachable by "since last issue"
        self._success(o, hours_ago=80)
        p = o.plan_newsletter()
        assert p["missed_schedule"] is True
        assert p["usual"]["candidates"] == 1
        assert (
            p["since_last_issue"]["candidates"] == 2
            and p["since_last_issue"]["window_hours"] == 80
        )

    def test_shipped_stories_are_excluded_from_both_counts(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, NEWSLETTER_MAX_STORY_AGE_HOURS="36")
        shipped = self._story(o, 1, hours_old=5)
        self._story(o, 3, hours_old=6)
        self._success(o, hours_ago=4, hashes=[shipped])
        p = o.plan_newsletter()
        assert p["usual"] == {
            "window_hours": 36.0,
            "candidates": 1,
            "already_sent": 1,
            "unscored": 0,
        }

    @pytest.mark.asyncio
    async def test_the_reported_window_hours_really_reach_the_missed_story(
        self, tmp_path, monkeypatch
    ):
        o = build(
            tmp_path,
            monkeypatch,
            NEWSLETTER_MAX_STORY_AGE_HOURS="36",
            NEWSLETTER_MIN_STORIES="1",
        )
        self._story(o, 3, hours_old=60)
        self._success(o, hours_ago=80)
        p = o.plan_newsletter()
        default = await o.run_newsletter(skip_publishing=True, force=True)
        chosen = await o.run_newsletter(
            skip_publishing=True, window_hours=p["since_last_issue"]["window_hours"]
        )
        assert default["status"] == "empty"
        assert [d["title"] for d in chosen["candidates"]] == ["Story 3"]

    def test_plan_makes_no_model_calls_and_records_nothing(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        self._story(o, 1, hours_old=5)
        o.importance_agent.score = AsyncMock()
        o.plan_newsletter()
        o.importance_agent.score.assert_not_awaited()
        assert o.run_log.runs == []


class TestClassificationFailureRelease:
    """A classification *failure* (rate limit, timeout) is not a decision: the
    article must return to the delta instead of being remembered as processed."""

    @pytest.mark.asyncio
    async def test_failed_article_is_released_for_retry(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        articles = [art(1), art(2), art(3)]
        # Discovery admits everything it hands over, as the real agent does.
        o.feed_ledger.observe(articles, "https://feed.example.com/rss")
        o.feed_ledger.mark_admitted(articles)
        o.article_tracker.record_discovered(articles)

        async def flaky(article):
            if "3" in article.title:
                raise RuntimeError("429 rate limit")
            return classify(article)

        o.discovery_agent.discover_articles = AsyncMock(return_value=articles)
        o._classify_article = AsyncMock(side_effect=flaky)

        result, _ = await o._research(step=0, total_steps=2)

        assert [e["title"] for e in result.classification_errors] == ["Story 3"]
        assert len(result.classified) == 2
        # Story 3 is eligible again; the two that were classified are not.
        assert [a.title for a in o.feed_ledger.unadmitted(articles)] == ["Story 3"]
        assert not o.article_tracker.is_processed(art(3))
        assert o.article_tracker.is_processed(art(1))
        # And it survived a reload of the ledger (release was saved).
        from wayr.services import FeedLedger

        reloaded = FeedLedger(ledger_file=o.settings.feed_ledger_file)
        assert [a.title for a in reloaded.unadmitted(articles)] == ["Story 3"]


class TestClassifierContentCap:
    @pytest.mark.asyncio
    async def test_classifiers_see_capped_excerpt_but_article_keeps_full_content(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch, CLASSIFICATION_MAX_CONTENT_CHARS="500")
        long = art(1)
        long.content = "w " * 2000  # 4000 chars
        seen = {}

        async def check(a):
            seen["industry"] = len(a.content)
            from wayr.models import IndustryFilterResult

            return IndustryFilterResult(belongs_to_industry=False, reasoning="stop")

        o.industry_classifier_agent.check = check
        out = await o._classify_article(long)
        assert seen["industry"] == 500
        assert len(out.original.content) == 4000

    @pytest.mark.asyncio
    async def test_no_cap_sends_everything(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        long = art(1)
        long.content = "w " * 2000
        seen = {}

        async def check(a):
            seen["industry"] = len(a.content)
            from wayr.models import IndustryFilterResult

            return IndustryFilterResult(belongs_to_industry=False, reasoning="stop")

        o.industry_classifier_agent.check = check
        await o._classify_article(long)
        assert seen["industry"] == 4000


class TestFreshnessSkipIsNewsletterOnly:
    def test_newsletter_mode_passes_cap_to_discovery(self, tmp_path, monkeypatch):
        o = build(
            tmp_path,
            monkeypatch,
            mode="newsletter",
            NEWSLETTER_MAX_STORY_AGE_HOURS="96",
        )
        assert o.discovery_agent.max_item_age_hours == 96

    @pytest.mark.parametrize("mode", ["news", "both"])
    def test_other_modes_do_not(self, tmp_path, monkeypatch, mode):
        o = build(tmp_path, monkeypatch, mode=mode, NEWSLETTER_MAX_STORY_AGE_HOURS="96")
        assert o.discovery_agent.max_item_age_hours is None


class TestWindowDedup:
    """The compile collapses same-event candidates and remembers the losers."""

    @pytest.mark.asyncio
    async def test_keeps_best_scored_and_marks_losers(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        cands = []
        for i, score in enumerate([0.5, 0.6, 0.4]):
            a = art(i)
            cands.append(
                {
                    "url_hash": f"h{i}",
                    "title": a.title,
                    "summary": "same event",
                    "rss_published_at": "2026-09-22T0%d:00:00+00:00" % i,
                    "importance": {"score": score},
                }
            )
            o.article_tracker.processed[f"h{i}"] = {
                "title": a.title,
                "last_stage": "category",
            }
        # indices 0 and 1 are one event; 2 stands alone
        o.dedup_agent.find_duplicates = AsyncMock(
            return_value={
                "duplicate_groups": [[0, 1]],
                "batch_duplicates_to_remove": {1},
            }
        )
        results = {}
        out = await o._dedup_window(cands, results)
        assert [c["url_hash"] for c in out] == ["h1", "h2"]  # 0.6 beat 0.5
        assert results["window_duplicates"] == 1
        assert o.article_tracker.processed["h0"]["duplicate_of"] == "h1"
        assert "duplicate_of" not in o.article_tracker.processed["h1"]

    @pytest.mark.asyncio
    async def test_marked_duplicates_leave_later_windows(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        for h in ("keep", "dup"):
            o.article_tracker.processed[h] = {
                "title": h,
                "last_stage": "category",
                "eligible_at": now.isoformat(),
                "rss_published_at": now.isoformat(),
            }
        o.article_tracker.mark_duplicates({"dup": "keep"})
        got = o.article_tracker.get_window_candidates(
            now - timedelta(hours=1), now + timedelta(hours=1), 96
        )
        assert [c["url_hash"] for c in got] == ["keep"]

    @pytest.mark.asyncio
    async def test_failure_passes_candidates_through(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        o.dedup_agent.find_duplicates = AsyncMock(side_effect=RuntimeError("429"))
        cands = [{"url_hash": "a", "title": "A"}, {"url_hash": "b", "title": "B"}]
        results = {}
        assert await o._dedup_window(cands, results) == cands
        assert results["window_duplicates"] == 0


class TestWindowDedupPrefersBrand:
    @pytest.mark.asyncio
    async def test_lower_scored_brand_post_is_the_keeper(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        cands = [
            {
                "url_hash": "press",
                "title": "OpenAI launches Astra",
                "summary": "s",
                "rss_published_at": "2026-09-22T01:00:00+00:00",
                "importance": {"score": 0.7},
                "source_origin": "press",
            },
            {
                "url_hash": "brand",
                "title": "GPT-6 Astra",
                "summary": "s",
                "rss_published_at": "2026-09-22T02:00:00+00:00",
                "importance": {"score": 0.5},
                "source_origin": "brand",
            },
        ]
        for c in cands:
            o.article_tracker.processed[c["url_hash"]] = {
                "title": c["title"],
                "last_stage": "category",
            }
        o.dedup_agent.find_duplicates = AsyncMock(
            return_value={
                "duplicate_groups": [[0, 1]],
                "batch_duplicates_to_remove": {1},
            }
        )
        out = await o._dedup_window(cands, {})
        assert [c["url_hash"] for c in out] == ["brand"]
        assert o.article_tracker.processed["press"]["duplicate_of"] == "brand"
        sent = o.dedup_agent.find_duplicates.await_args.kwargs["batch_titles"]
        assert [d["origin"] for d in sent] == ["press", "brand"]


class TestDiscoveryDedupIsOffInNewsletterMode:
    def test_newsletter_mode_skips_discovery_dedup(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch, mode="newsletter")
        assert o.dedup_agent is not None  # still built: the window dedup uses it
        assert o.discovery_agent.llm_dedup is False

    @pytest.mark.parametrize("mode", ["news", "both"])
    def test_other_modes_keep_it(self, tmp_path, monkeypatch, mode):
        o = build(tmp_path, monkeypatch, mode=mode)
        assert o.discovery_agent.llm_dedup is True


class TestWindowDedupAgainstShipped:
    @pytest.mark.asyncio
    async def test_shipped_story_drops_its_new_coverage(self, tmp_path, monkeypatch):
        from wayr.services import NewsletterRun

        o = build(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        o.article_tracker.processed["old"] = {
            "title": "OpenAI launches GPT-6",
            "summary": "s",
            "last_stage": "category",
        }
        o.article_tracker.processed["new"] = {
            "title": "GPT-6 is here, says OpenAI",
            "summary": "s",
            "last_stage": "category",
        }
        o.run_log.record(
            NewsletterRun(
                window_start=(now - timedelta(days=1)).isoformat(),
                window_end=now.isoformat(),
                status="success",
                story_hashes=["old"],
            )
        )
        cands = [
            {
                "url_hash": "new",
                "title": "GPT-6 is here, says OpenAI",
                "summary": "s",
                "importance": {"score": 0.9},
            }
        ]
        o.dedup_agent.find_duplicates = AsyncMock(
            return_value={
                "duplicate_groups": [[0, 1]],
                "batch_duplicates_to_remove": {0},
            }
        )
        out = await o._dedup_window(cands, {})
        assert out == []
        assert o.article_tracker.processed["new"]["duplicate_of"] == "old"
        kw = o.dedup_agent.find_duplicates.await_args.kwargs
        assert kw["cached_titles"] == [
            {
                "index": 1,
                "title": "OpenAI launches GPT-6",
                "summary": "s",
                "url_hash": "old",
            }
        ]

    @pytest.mark.asyncio
    async def test_nothing_shipped_means_no_cached_titles(self, tmp_path, monkeypatch):
        o = build(tmp_path, monkeypatch)
        cands = [
            {"url_hash": "a", "title": "A", "summary": ""},
            {"url_hash": "b", "title": "B", "summary": ""},
        ]
        o.dedup_agent.find_duplicates = AsyncMock(
            return_value={"duplicate_groups": [], "batch_duplicates_to_remove": set()}
        )
        await o._dedup_window(cands, {})
        assert o.dedup_agent.find_duplicates.await_args.kwargs["cached_titles"] is None


class TestWindowDedupWithIncludeSent:
    @pytest.mark.asyncio
    async def test_shipped_story_in_window_is_not_its_own_cached_twin(
        self, tmp_path, monkeypatch
    ):
        from wayr.services import NewsletterRun

        o = build(tmp_path, monkeypatch)
        now = datetime.now(timezone.utc)
        for h in ("a", "b"):
            o.article_tracker.processed[h] = {
                "title": h.upper(),
                "summary": "s",
                "last_stage": "category",
            }
        o.run_log.record(
            NewsletterRun(
                window_start=(now - timedelta(days=1)).isoformat(),
                window_end=now.isoformat(),
                status="success",
                story_hashes=["a"],
            )
        )
        # --include-sent: "a" is a candidate again, "b" is new
        cands = [
            {"url_hash": "a", "title": "A", "summary": "s"},
            {"url_hash": "b", "title": "B", "summary": "s"},
        ]
        o.dedup_agent.find_duplicates = AsyncMock(
            return_value={"duplicate_groups": [], "batch_duplicates_to_remove": set()}
        )
        out = await o._dedup_window(cands, {})
        assert [c["url_hash"] for c in out] == ["a", "b"]
        assert o.dedup_agent.find_duplicates.await_args.kwargs["cached_titles"] is None


class TestBlurbFallsBackToFeedSummary:
    @pytest.mark.asyncio
    async def test_feed_summary_used_when_nothing_to_summarize(
        self, tmp_path, monkeypatch
    ):
        from wayr.models.newsletter import NewsletterStory, Tier

        o = build(tmp_path, monkeypatch)
        o.article_tracker.processed["h"] = {
            "title": "T",
            "summary": "The feed's own description.",
            "last_stage": "category",
        }
        o.summary_agent.generate_summary_from_source = AsyncMock(return_value="")
        st = NewsletterStory(
            url_hash="h",
            url="https://x/1",
            source="S",
            source_title="T",
            tier=Tier.FEATURE,
        )
        assert await o._blurb_for(st) == "The feed's own description."

    @pytest.mark.asyncio
    async def test_real_blurb_wins(self, tmp_path, monkeypatch):
        from wayr.models.newsletter import NewsletterStory, Tier

        o = build(tmp_path, monkeypatch)
        o.article_tracker.processed["h"] = {
            "title": "T",
            "summary": "feed",
            "content": "w " * 100,
            "last_stage": "category",
        }
        o.summary_agent.generate_summary_from_source = AsyncMock(
            return_value="written blurb"
        )
        st = NewsletterStory(
            url_hash="h",
            url="https://x/1",
            source="S",
            source_title="T",
            tier=Tier.FEATURE,
        )
        assert await o._blurb_for(st) == "written blurb"


class TestResume:
    """A run killed between discovery and the classification commit leaves
    entries at stage=discovered; the next run classifies them."""

    @pytest.mark.asyncio
    async def test_unclassified_entries_are_revived_and_classified(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch)
        # An earlier run: admitted and recorded as discovered, then died.
        earlier = [art(10), art(11)]
        o.article_tracker.record_discovered(earlier)
        assert len(o.article_tracker.get_unclassified()) == 2

        # This run discovers one fresh article; the page re-extract works for
        # one stuck entry and fails for the other, whose feed summary is thin.
        wire(o, [art(1)])
        with patch("wayr.orchestrator.ContentExtractor") as ext:
            ext.return_value.extract = AsyncMock(
                side_effect=lambda url: "w " * 80 if url.endswith("/10") else ""
            )
            r, _ = await o._research(0, 3)

        assert [a.title for a in r.discovered] == ["Story 1"]
        assert r.resumed == 1
        classified = {c.original.title for c in r.classified}
        assert classified == {"Story 1", "Story 10"}
        # Story 10 advanced past 'discovered'; Story 11 stays for the next run.
        assert [e["title"] for e in o.article_tracker.get_unclassified()] == [
            "Story 11"
        ]

    @pytest.mark.asyncio
    async def test_fresh_discovery_is_not_double_counted_as_resume(
        self, tmp_path, monkeypatch
    ):
        o = build(tmp_path, monkeypatch)
        o.article_tracker.record_discovered([art(1)])
        wire(o, [art(1)])  # the same URL comes back from the feed this run
        with patch("wayr.orchestrator.ContentExtractor") as ext:
            ext.return_value.extract = AsyncMock(return_value="w " * 80)
            r, _ = await o._research(0, 3)
        assert r.resumed == 0
        assert len(r.classified) == 1
        ext.return_value.extract.assert_not_awaited()
