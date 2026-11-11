"""ArticleTracker keeps the feed summary so cross-batch dedup sees more than a headline."""

from datetime import datetime, timezone

from wayr.models import RSSArticle
from wayr.services.article_tracker import SUMMARY_MAX_CHARS, ArticleTracker


def _article(url: str, summary):
    return RSSArticle(
        title="Anthropic merges Claude chat and Cowork in one interface",
        url=url,
        source="TechCrunch",
        published_date=datetime(2026, 9, 16, 16, 30, tzinfo=timezone.utc),
        summary=summary,
    )


def test_summary_is_cleaned_and_stored_on_discovery(tmp_path):
    tracker = ArticleTracker(cache_file=str(tmp_path / "cache.json"))
    a = _article("https://example.com/a", "<p>Pro &amp; Max first.</p>" + "x" * 900)
    tracker.record_discovered([a])

    entry = next(iter(tracker.processed.values()))
    assert entry["summary"].startswith("Pro & Max first. ")
    assert len(entry["summary"]) == SUMMARY_MAX_CHARS


def test_recent_titles_carry_summary(tmp_path):
    tracker = ArticleTracker(cache_file=str(tmp_path / "cache.json"))
    a = _article("https://example.com/a", "Initially for Pro and Max subscribers.")
    tracker.record_discovered([a])
    tracker.record_published([a])

    recent = tracker.get_recent_titles(days=7)
    assert recent == [
        {
            "title": a.title,
            "hash": recent[0]["hash"],
            "summary": "Initially for Pro and Max subscribers.",
        }
    ]


def test_recent_titles_tolerate_legacy_entries_without_summary(tmp_path):
    tracker = ArticleTracker(cache_file=str(tmp_path / "cache.json"))
    a = _article("https://example.com/a", None)
    tracker.record_discovered([a])
    tracker.record_published([a])
    for entry in tracker.processed.values():
        entry.pop("summary", None)  # pre-summary cache shape

    assert tracker.get_recent_titles(days=7)[0]["summary"] == ""


def test_source_origin_is_stored_at_discovery(tmp_path):
    tracker = ArticleTracker(cache_file=str(tmp_path / "cache.json"))
    a = _article("https://example.com/a", "s")
    a.source_origin = "brand"
    b = _article("https://example.com/b", "s")  # untyped → None, never a crash
    tracker.record_discovered([a, b])
    origins = {e["url"]: e["source_origin"] for e in tracker.processed.values()}
    assert origins == {"https://example.com/a": "brand", "https://example.com/b": None}


def test_entries_without_the_key_still_load_and_window(tmp_path):
    import json
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps(
            {
                "h1": {
                    "title": "old",
                    "url": "https://example.com/old",
                    "created_at": now.isoformat(),
                    "last_stage": "category",
                    "eligible_at": now.isoformat(),
                    "rss_published_at": now.isoformat(),
                }
            }
        )
    )
    tracker = ArticleTracker(cache_file=str(cache))
    got = tracker.get_window_candidates(
        now - timedelta(hours=1), now + timedelta(hours=1), 96
    )
    assert [c["url_hash"] for c in got] == ["h1"] and got[0].get(
        "source_origin"
    ) is None


def test_a_story_is_never_marked_as_its_own_duplicate(tmp_path):
    tracker = ArticleTracker(cache_file=str(tmp_path / "cache.json"))
    tracker.processed["h"] = {"title": "x", "last_stage": "category"}
    assert tracker.mark_duplicates({"h": "h"}) == 0
    assert "duplicate_of" not in tracker.processed["h"]
