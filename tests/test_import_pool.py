"""Tests for seeding the local pool from Supabase: row mapping, bulk import, paginated read, CLI."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from wayr import cli
from wayr.services import article_tracker as at
from wayr.services.article_tracker import ArticleTracker, entry_from_pipeline_row
from wayr.services import supabase_store as ss

from datetime import datetime, timedelta, timezone

# Fixtures are "yesterday", not a fixed date: the tracker purges entries past its
# 30-day retention on save, so a pinned date would start failing a month later.
_Y = datetime.now(timezone.utc) - timedelta(days=1)


def _ts(hour: int, minute: int = 0) -> str:
    return _Y.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def row(**over):
    base = dict(
        url_hash="h1",
        created_at=_ts(10, 0),
        updated_at=_ts(11, 0),
        eligible_at=None,
        rss_published_at=_ts(9, 30),
        region="us",
        title="T",
        source_url="https://ex.com/t",
        source_tag="Ex",
        raw_content="body " * 10,
        status="candidate",
        last_stage="category",
        industry_classification=True,
        news_classification=True,
        article_type="news",
        categories=["AI", "Startup"],
        importance=None,
        wordpress_url=None,
    )
    base.update(over)
    return base


class TestRowMapping:
    def test_full_row(self):
        h, e = entry_from_pipeline_row(row())
        assert h == "h1"
        assert (e["title"], e["url"], e["source"], e["last_stage"]) == (
            "T",
            "https://ex.com/t",
            "Ex",
            "category",
        )
        assert e["eligible_at"] == e["created_at"]  # null in DB → created_at
        assert e["rss_published_at"] == _ts(9, 30)
        assert e["industry"] == {"belongs": True, "detected": None, "reasoning": ""}
        assert e["news_type"]["content_type"] == "news"
        assert e["category"] == {
            "primary": "AI",
            "secondary": "Startup",
            "reasoning": "",
        }
        assert e["content"].startswith("body ")
        assert "importance" not in e

    def test_published_row_keeps_stage_and_wordpress_url(self):
        _, e = entry_from_pipeline_row(
            row(last_stage="published", wordpress_url="https://wayr/p/1")
        )
        assert (
            e["last_stage"] == "published" and e["wordpress_url"] == "https://wayr/p/1"
        )

    def test_importance_passes_through(self):
        _, e = entry_from_pipeline_row(
            row(importance={"score": 0.7, "reasoning": "r", "scored_at": "s"})
        )
        assert e["importance"] == {"score": 0.7, "reasoning": "r", "scored_at": "s"}

    def test_minimal_legacy_row(self):
        h, e = entry_from_pipeline_row(
            {
                "url_hash": "h9",
                "source_url": "https://ex.com/9",
                "created_at": "2026-09-01T00:00:00+00:00",
            }
        )
        assert (
            e["last_stage"] == "discovered"
            and e["eligible_at"] == "2026-09-01T00:00:00+00:00"
        )
        assert "industry" not in e and "category" not in e and "content" not in e

    def test_content_capped(self):
        _, e = entry_from_pipeline_row(
            row(raw_content="x" * (at.CONTENT_MAX_CHARS + 500))
        )
        assert len(e["content"]) == at.CONTENT_MAX_CHARS

    def test_single_category_and_unknown_stage(self):
        _, e = entry_from_pipeline_row(row(categories=["Hardware"], last_stage="weird"))
        assert e["category"]["secondary"] == "" and e["last_stage"] == "discovered"

    def test_rows_without_hash_or_url_are_skipped(self):
        assert entry_from_pipeline_row(row(url_hash=None)) is None
        assert entry_from_pipeline_row(row(source_url="")) is None


class TestImportEntries:
    def test_adds_and_keeps_existing_unless_overwrite(self, tmp_path):
        t = ArticleTracker(str(tmp_path / "p.json"))
        assert (
            t.import_entries(
                {
                    "a": {
                        "title": "old",
                        "last_stage": "category",
                        "created_at": _ts(0, 0),
                    }
                }
            )
            == 1
        )
        assert (
            t.import_entries(
                {
                    "a": {
                        "title": "new",
                        "last_stage": "category",
                        "created_at": _ts(0, 0),
                    },
                    "b": {
                        "title": "b",
                        "last_stage": "category",
                        "created_at": _ts(0, 0),
                    },
                }
            )
            == 1
        )
        assert t.processed["a"]["title"] == "old"
        assert (
            t.import_entries(
                {
                    "a": {
                        "title": "new",
                        "last_stage": "category",
                        "created_at": _ts(0, 0),
                    }
                },
                overwrite=True,
            )
            == 1
        )
        assert t.processed["a"]["title"] == "new"
        assert ArticleTracker(str(tmp_path / "p.json")).processed.keys() == {
            "a",
            "b",
        }  # persisted

    def test_imported_published_story_is_a_window_candidate(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        t = ArticleTracker(str(tmp_path / "p.json"))
        h, e = entry_from_pipeline_row(
            row(
                last_stage="published",
                created_at=now.isoformat(),
                rss_published_at=now.isoformat(),
            )
        )
        t.import_entries({h: e})
        cands = t.get_window_candidates(
            now - timedelta(hours=24), now + timedelta(seconds=1), 36
        )
        assert [c["title"] for c in cands] == ["T"] and cands[0]["content"]


class FakeClient:
    """Builder chain that records filters and serves pages from `rows`."""

    def __init__(self, rows):
        self.rows, self.calls, self.filters = rows, 0, []

    def table(self, name):
        self._table = name
        return self

    def __getattr__(self, name):  # select/gte/in_/order/eq/range → chain
        def _m(*a, **k):
            self.filters.append((name, a))
            if name == "range":
                self._range = a
            return self

        return _m

    def execute(self):
        self.calls += 1
        lo, hi = self._range
        return SimpleNamespace(data=self.rows[lo : hi + 1])


class TestFetchPool:
    def _store(self, rows):
        s = ss.SupabasePipelineStore.__new__(ss.SupabasePipelineStore)
        s.client, s.region = FakeClient(rows), "us"
        return s

    def test_paginates_and_filters(self, monkeypatch):
        monkeypatch.setattr(ss, "POOL_PAGE_SIZE", 2)
        s = self._store([row(url_hash=f"h{i}") for i in range(5)])
        out = s.fetch_pool(days=3)
        assert [r["url_hash"] for r in out] == ["h0", "h1", "h2", "h3", "h4"]
        assert s.client.calls == 3  # 2 + 2 + 1
        names = [f[0] for f in s.client.filters]
        assert {"select", "gte", "in_", "order", "range", "eq"} <= set(names)
        assert ("eq", ("region", "us")) in s.client.filters
        assert any(
            f == ("in_", ("last_stage", ["category", "published"]))
            for f in s.client.filters
        )

    def test_disabled_store_returns_empty(self):
        s = ss.SupabasePipelineStore.__new__(ss.SupabasePipelineStore)
        s.client, s.region = None, None
        assert s.fetch_pool() == []


class TestCli:
    def test_import_pool_writes_the_configured_cache(self, tmp_path):
        settings = SimpleNamespace(
            supabase_url="u",
            supabase_secret_key="k",
            region="us",
            article_cache_file=str(tmp_path / "pool.json"),
            article_retention_days=30,
        )
        store = MagicMock()
        store.enabled = True
        store.fetch_pool.return_value = [
            row(url_hash="a"),
            row(url_hash="b", last_stage="published"),
            {"url_hash": None},
        ]
        with patch.object(cli, "get_settings", return_value=settings), patch(
            "wayr.services.SupabasePipelineStore", return_value=store
        ):
            cli.cmd_import_pool(days=3)
        store.fetch_pool.assert_called_once_with(
            days=3, stages=("category", "published")
        )
        pool = ArticleTracker(str(tmp_path / "pool.json")).processed
        assert set(pool) == {"a", "b"} and pool["b"]["last_stage"] == "published"

    def test_requires_supabase(self, tmp_path):
        settings = SimpleNamespace(
            supabase_url=None,
            supabase_secret_key=None,
            region=None,
            article_cache_file=str(tmp_path / "pool.json"),
            article_retention_days=30,
        )
        with patch.object(cli, "get_settings", return_value=settings):
            with pytest.raises(SystemExit) as e:
                cli.cmd_import_pool()
            assert e.value.code == 1


def test_source_origin_maps_from_row():
    _, e = entry_from_pipeline_row(row(source_origin="brand"))
    assert e["source_origin"] == "brand"
    _, e = entry_from_pipeline_row(row())
    assert e["source_origin"] is None
