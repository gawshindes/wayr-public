"""Tests for FeedLedger — feed items, admission, conditional-GET state, retention."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from wayr.models import RSSArticle
from wayr.services import FeedLedger

FEED = "https://example.com/feed"


def art(n: int, **kw) -> RSSArticle:
    base = dict(
        title=f"Story {n}",
        url=f"https://example.com/story-{n}",
        source="Example",
        published_date=datetime(2026, 9, 1, 12, 0),
        summary="<p>Hello <b>world</b></p>",
    )
    base.update(kw)
    return RSSArticle(**base)


@pytest.fixture
def ledger(tmp_path):
    return FeedLedger(ledger_file=str(tmp_path / "ledger.json"), retention_days=30)


class TestObserve:
    def test_new_vs_known(self, ledger):
        new, known = ledger.observe([art(1), art(2)], FEED)
        assert [a.title for a in new] == ["Story 1", "Story 2"] and known == 0
        new, known = ledger.observe([art(1), art(3)], FEED)
        assert [a.title for a in new] == ["Story 3"] and known == 1

    def test_first_seen_preserved_last_seen_bumped(self, ledger, monkeypatch):
        ledger.observe([art(1)], FEED)
        (row,) = ledger.items.values()
        first, last = row["first_seen_at"], row["last_seen_at"]
        monkeypatch.setattr(
            "wayr.services.feed_ledger._now", lambda: "2099-01-01T00:00:00+00:00"
        )
        ledger.observe([art(1)], FEED)
        assert row["first_seen_at"] == first
        assert row["last_seen_at"] == "2099-01-01T00:00:00+00:00" != last

    def test_row_contents(self, ledger):
        ledger.observe([art(1)], FEED)
        (row,) = ledger.items.values()
        assert row["feed_url"] == FEED
        assert row["rss_summary"] == "Hello world"  # tags stripped
        assert row["rss_published_at"].endswith("+00:00")  # naive → UTC
        assert row["admitted"] is False and row["admitted_at"] is None

    def test_summary_truncated(self, ledger):
        ledger.observe([art(1, summary="x" * 2000)], FEED)
        (row,) = ledger.items.values()
        assert len(row["rss_summary"]) == 500


class TestAdmission:
    """The delta is 'not admitted', not 'not seen' — capped items stay eligible."""

    def test_unadmitted_includes_seen_but_capped(self, ledger):
        batch = [art(1), art(2), art(3)]
        ledger.observe(batch, FEED)
        ledger.mark_admitted(batch[:1])  # cap of 1
        remaining = ledger.unadmitted(batch)
        assert [a.title for a in remaining] == ["Story 2", "Story 3"]

    def test_unadmitted_treats_unknown_as_eligible(self, ledger):
        assert len(ledger.unadmitted([art(9)])) == 1

    def test_mark_admitted_is_idempotent_and_counts_changes(self, ledger):
        ledger.observe([art(1), art(2)], FEED)
        assert ledger.mark_admitted([art(1)]) == 1
        assert ledger.mark_admitted([art(1), art(2)]) == 1
        assert ledger.items[next(iter(ledger.items))]["admitted_at"] is not None

    def test_release_puts_article_back_into_delta(self, ledger):
        batch = [art(1), art(2)]
        ledger.observe(batch, FEED)
        ledger.mark_admitted(batch)
        assert ledger.unadmitted(batch) == []
        assert ledger.release([art(1), art(9)]) == 1  # unknown url is a no-op
        assert [a.title for a in ledger.unadmitted(batch)] == ["Story 1"]
        assert ledger.release([art(1)]) == 0  # idempotent


class TestFeedState:
    def test_round_trip_validators(self, ledger):
        assert ledger.feed_state(FEED) == {}
        ledger.record_fetch(
            FEED, status=200, etag='"abc"', last_modified="Mon", item_count=7
        )
        st = ledger.feed_state(FEED)
        assert st["etag"] == '"abc"' and st["last_modified"] == "Mon"
        assert st["last_status"] == 200 and st["last_item_count"] == 7
        assert st["consecutive_failures"] == 0

    def test_304_keeps_previous_validators(self, ledger):
        ledger.record_fetch(
            FEED, status=200, etag='"abc"', last_modified="Mon", item_count=7
        )
        ledger.record_fetch(FEED, status=304)
        st = ledger.feed_state(FEED)
        assert st["etag"] == '"abc"' and st["last_item_count"] == 7
        assert st["last_status"] == 304

    def test_failures_count_and_reset(self, ledger):
        ledger.record_fetch(FEED, status="error")
        ledger.record_fetch(FEED, status="error")
        assert ledger.feed_state(FEED)["consecutive_failures"] == 2
        ledger.record_fetch(FEED, status=304)
        assert ledger.feed_state(FEED)["consecutive_failures"] == 0

    def test_stats_flags_failing_feeds(self, ledger):
        for _ in range(3):
            ledger.record_fetch(FEED, status="error")
        assert ledger.stats()["failing_feeds"] == [FEED]


class TestRetention:
    def test_purges_on_last_seen_not_first_seen(self, ledger):
        ledger.observe([art(1), art(2)], FEED)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        rows = list(ledger.items.values())
        rows[0]["first_seen_at"] = old  # old but still in the feed
        rows[1]["first_seen_at"] = old
        rows[1]["last_seen_at"] = old  # feed has forgotten it
        assert ledger.purge() == 1
        assert [r["title"] for r in ledger.items.values()] == ["Story 1"]


class TestPersistence:
    def test_save_and_reload(self, tmp_path):
        path = str(tmp_path / "ledger.json")
        a = FeedLedger(ledger_file=path)
        a.observe([art(1)], FEED)
        a.record_fetch(FEED, status=200, etag='"e"', item_count=1)
        a.mark_admitted([art(1)])
        a.save()
        b = FeedLedger(ledger_file=path)
        assert b.feed_state(FEED)["etag"] == '"e"'
        assert b.unadmitted([art(1)]) == []
        assert not (tmp_path / "ledger.json.tmp").exists()

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text("{not json")
        ledger = FeedLedger(ledger_file=str(path))
        assert ledger.items == {} and ledger.feeds == {}

    def test_reload_purges(self, tmp_path):
        path = tmp_path / "ledger.json"
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        path.write_text(
            json.dumps(
                {"feeds": {}, "items": {"h": {"last_seen_at": old, "title": "x"}}}
            )
        )
        assert FeedLedger(ledger_file=str(path), retention_days=90).items == {}


class TestSourceOrigin:
    def test_new_rows_carry_kind_and_old_rows_backfill(self, ledger):
        a = art(1)
        ledger.observe(
            [a], FEED
        )  # origin unknown → None, like a pre-typed-sources ledger
        row = ledger.items[next(iter(ledger.items))]
        assert row["source_origin"] is None
        a.source_origin = "brand"
        ledger.observe([a], FEED)  # same URL seen again from a typed source
        assert row["source_origin"] == "brand"
        b = art(2, source_origin="press")
        ledger.observe([b], FEED)
        assert [r["source_origin"] for r in ledger.items.values()] == [
            "brand",
            "press",
        ]
