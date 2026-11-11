"""Tests for `wayr newsletter` and `wayr reset` — the orchestrator is patched, nothing runs."""

import csv
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wayr import cli


def settings_for(tmp_path):
    return SimpleNamespace(
        newsletter_runs_file=str(tmp_path / "data" / "runs.json"),
        feed_ledger_file=str(tmp_path / "data" / "ledger.json"),
        article_cache_file=str(tmp_path / "data" / "processed.json"),
    )


def results(status="preview", **over):
    base = dict(
        status=status,
        window_source="bootstrap",
        window_start="2026-09-15T00:00:00+00:00",
        window_end="2026-09-16T00:00:00+00:00",
        candidates=[
            {
                "url_hash": "a",
                "title": "A",
                "source": "S",
                "url": "u/a",
                "importance": {"score": 0.9, "reasoning": "big"},
                "category": {"primary": "AI"},
                "eligible_at": "e",
                "rss_published_at": "p",
                "content": "SECRET-LONG-CONTENT",
            },
            {
                "url_hash": "b",
                "title": "B",
                "source": "S",
                "url": "u/b",
                "importance": {"score": 0.4, "reasoning": "meh"},
            },
            {"url_hash": "c", "title": "C", "source": "S", "url": "u/c"},
        ],
        featured=[{"url_hash": "a"}],
        roundup=[{"url_hash": "b"}],
        dropped=[],
        unscored=[{"url_hash": "c"}],
        counts={"featured": 1, "roundup": 1, "dropped": 0, "unscored": 1},
        sections=[
            {
                "name": "Top Story",
                "style": "feature",
                "placeholder": False,
                "stories": 1,
            }
        ],
        unplaced=[],
        html="<div>ISSUE HTML</div>",
        compile={"llm_generated": True, "summaries": 1, "compile_error": None},
        publisher=None,
        external_id=None,
        draft_url=None,
        errors=[],
    )
    base.update(over)
    return base


@pytest.fixture
def orch(tmp_path):
    o = MagicMock()
    o.run_prework = AsyncMock(
        return_value={"discovered": [1, 2], "news": [1], "scored": [1]}
    )
    o.run_newsletter = AsyncMock(return_value=results())
    with patch.object(cli, "WAYROrchestrator", return_value=o), patch.object(
        cli, "get_settings", return_value=settings_for(tmp_path)
    ):
        yield o


class TestNewsletterCommand:
    @pytest.mark.asyncio
    async def test_preview_writes_html_and_pushes_nothing(self, orch, tmp_path):
        await cli.cmd_newsletter(skip_publishing=True)
        orch.run_newsletter.assert_awaited_once_with(
            skip_publishing=True, force=False, window_hours=None, include_sent=False
        )
        out = tmp_path / "data" / "newsletter_preview_2026-09-16.html"
        assert out.read_text() == "<div>ISSUE HTML</div>"

    @pytest.mark.asyncio
    async def test_dump_candidates_writes_csv_with_tiers_and_implies_preview(
        self, orch, tmp_path
    ):
        await cli.cmd_newsletter(dump_candidates="")
        assert orch.run_newsletter.await_args.kwargs["skip_publishing"] is True
        rows = list(
            csv.DictReader(
                (tmp_path / "data" / "newsletter_candidates_2026-09-16.csv").open()
            )
        )
        assert [(r["tier"], r["title"], r["score"]) for r in rows] == [
            ("featured", "A", "0.9"),
            ("roundup", "B", "0.4"),
            ("unscored", "C", ""),
        ]
        assert rows[0]["category"] == "AI" and rows[0]["reasoning"] == "big"
        assert (
            "SECRET-LONG-CONTENT"
            not in (
                tmp_path / "data" / "newsletter_candidates_2026-09-16.csv"
            ).read_text()
        )

    @pytest.mark.asyncio
    async def test_dump_candidates_to_explicit_path(self, orch, tmp_path):
        target = tmp_path / "out" / "c.csv"
        await cli.cmd_newsletter(dump_candidates=str(target))
        assert target.exists()

    @pytest.mark.asyncio
    async def test_with_prework_runs_prework_first(self, orch):
        await cli.cmd_newsletter(with_prework=True, skip_publishing=True)
        orch.run_prework.assert_awaited_once()
        assert orch.mock_calls.index(("run_prework", (), {})) < orch.mock_calls.index(
            (
                "run_newsletter",
                (),
                {
                    "skip_publishing": True,
                    "force": False,
                    "window_hours": None,
                    "include_sent": False,
                },
            )
        )

    @pytest.mark.asyncio
    async def test_flags_pass_through(self, orch):
        await cli.cmd_newsletter(force=True, window_hours=48, skip_publishing=True)
        orch.run_newsletter.assert_awaited_once_with(
            skip_publishing=True, force=True, window_hours=48, include_sent=False
        )

    @pytest.mark.asyncio
    async def test_published_reports_draft_and_exits_zero(self, orch):
        orch.run_newsletter.return_value = results(
            "published", publisher="beehiiv", external_id="p1", draft_url="https://b/p1"
        )
        await cli.cmd_newsletter()  # returns normally
        orch.run_newsletter.assert_awaited_once_with(
            skip_publishing=False, force=False, window_hours=None, include_sent=False
        )

    @pytest.mark.asyncio
    async def test_failed_exits_one(self, orch):
        orch.run_newsletter.return_value = results(
            "failed", publisher="beehiiv", errors=["beehiiv 500"]
        )
        with pytest.raises(SystemExit) as e:
            await cli.cmd_newsletter()
        assert e.value.code == 1

    @pytest.mark.asyncio
    async def test_already_sent_and_empty_return_normally(self, orch, tmp_path):
        for status in ("already_sent", "empty"):
            orch.run_newsletter.return_value = results(status, candidates=[], html=None)
            await cli.cmd_newsletter(skip_publishing=True)
        assert (
            not list((tmp_path / "data").glob("*.html"))
            if (tmp_path / "data").exists()
            else True
        )

    @pytest.mark.asyncio
    async def test_orchestrator_exception_exits_one(self, orch):
        orch.run_newsletter.side_effect = RuntimeError("schema broken")
        with pytest.raises(SystemExit) as e:
            await cli.cmd_newsletter()
        assert e.value.code == 1


class TestResetCommand:
    def _files(self, tmp_path):
        s = settings_for(tmp_path)
        paths = [s.feed_ledger_file, s.article_cache_file, s.newsletter_runs_file]
        (tmp_path / "data").mkdir()
        for p in paths[:2]:  # run log intentionally absent
            open(p, "w").write("{}")
        return s, paths

    def test_requires_a_target(self, tmp_path):
        with patch.object(cli, "get_settings", return_value=settings_for(tmp_path)):
            with pytest.raises(SystemExit) as e:
                cli.cmd_reset(newsletter=False)
            assert e.value.code == 2

    def test_yes_deletes_existing_only(self, tmp_path):
        s, paths = self._files(tmp_path)
        with patch.object(cli, "get_settings", return_value=s):
            cli.cmd_reset(newsletter=True, yes=True)
        assert not any(__import__("os").path.exists(p) for p in paths)

    def test_refuses_without_yes_when_not_a_tty(self, tmp_path):
        s, paths = self._files(tmp_path)
        with patch.object(cli, "get_settings", return_value=s), patch.object(
            cli.sys.stdin, "isatty", return_value=False
        ):
            with pytest.raises(SystemExit) as e:
                cli.cmd_reset(newsletter=True)
            assert e.value.code == 2
        assert __import__("os").path.exists(paths[0])  # nothing deleted

    def test_prompt_no_aborts(self, tmp_path):
        s, paths = self._files(tmp_path)
        with patch.object(cli, "get_settings", return_value=s), patch.object(
            cli.sys.stdin, "isatty", return_value=True
        ), patch("builtins.input", return_value="n"):
            cli.cmd_reset(newsletter=True)
        assert __import__("os").path.exists(paths[0])

    def test_nothing_to_reset(self, tmp_path):
        with patch.object(cli, "get_settings", return_value=settings_for(tmp_path)):
            cli.cmd_reset(newsletter=True, yes=True)  # no files, no error


class TestIncludeSent:
    @pytest.mark.asyncio
    async def test_flag_passes_through(self, orch):
        await cli.cmd_newsletter(skip_publishing=True, include_sent=True)
        assert orch.run_newsletter.await_args.kwargs["include_sent"] is True


class TestPlanFlag:
    @pytest.mark.asyncio
    async def test_plan_prints_and_runs_nothing(self, orch, capsys):
        orch.plan_newsletter = MagicMock(
            return_value={
                "now": "n",
                "last_successful_at": "2026-09-14T17:00:00+00:00",
                "hours_since_last": 80.0,
                "sent_today": False,
                "max_story_age_hours": 36,
                "missed_schedule": True,
                "usual": {
                    "window_hours": 36.0,
                    "candidates": 21,
                    "already_sent": 9,
                    "unscored": 0,
                },
                "since_last_issue": {
                    "window_hours": 80.0,
                    "candidates": 58,
                    "already_sent": 9,
                    "unscored": 2,
                },
            }
        )
        await cli.cmd_newsletter(plan=True)
        orch.run_newsletter.assert_not_awaited()
        orch.run_prework.assert_not_awaited()
