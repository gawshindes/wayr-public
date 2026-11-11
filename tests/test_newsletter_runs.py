"""Tests for the newsletter run log and window computation."""

import json
from datetime import date, datetime, timedelta, timezone

from wayr.services.newsletter_runs import (
    NewsletterRun,
    NewsletterRunLog,
    compute_window,
)

T0 = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)
H = timedelta(hours=1)


class TestComputeWindow:
    """One knob: the story-age cap. No period, no hidden outage clamp."""

    def test_first_run_reaches_back_the_story_age_cap(self):
        assert compute_window(T0, None, 36) == (T0 - 36 * H, T0)

    def test_default_starts_at_the_last_issue(self):
        last = T0 - 20 * H
        assert compute_window(T0, last, 36) == (last, T0)

    def test_default_never_reaches_past_the_story_age_cap(self):
        """After a long gap the default window is bounded by freshness, which drops
        older stories anyway — asking for more is an explicit override."""
        start, end = compute_window(T0, T0 - 72 * H, 36)
        assert start == T0 - 36 * H and end == T0

    def test_override_reaches_back_as_far_as_asked(self):
        assert compute_window(T0, T0 - 2 * H, 36, override_hours=72) == (
            T0 - 72 * H,
            T0,
        )

    def test_first_and_later_runs_use_the_same_bound(self):
        """The old design looked back 24h on the first run and 36h afterwards."""
        first = compute_window(T0, None, 36)[0]
        later = compute_window(T0, T0 - 500 * H, 36)[0]
        assert first == later

    def test_watermark_in_the_future_gives_empty_window(self):
        start, end = compute_window(T0, T0 + H, 36)
        assert start == end == T0

    def test_end_is_always_now(self):
        for last in (None, T0 - H, T0 - 100 * H):
            assert compute_window(T0, last, 36)[1] == T0


def run(status="success", end=T0, created=T0, **kw) -> NewsletterRun:
    return NewsletterRun(
        window_start=(end - 24 * H).isoformat(),
        window_end=end.isoformat(),
        status=status,
        created_at=created.isoformat(),
        **kw,
    )


class TestRunLog:
    def test_empty_log(self, tmp_path):
        log = NewsletterRunLog(str(tmp_path / "runs.json"))
        assert log.last_successful_window_end() is None
        assert not log.has_success_on(T0.date())
        assert log.stats() == {
            "runs": 0,
            "successes": 0,
            "last_successful_window_end": None,
        }

    def test_watermark_is_max_successful_window_end(self, tmp_path):
        log = NewsletterRunLog(str(tmp_path / "runs.json"))
        log.record(run(end=T0 - 48 * H))
        log.record(run(end=T0 - 24 * H))
        log.record(run(status="failed", end=T0))  # must not advance
        log.record(run(status="preview", end=T0))  # must not advance
        assert log.last_successful_window_end() == T0 - 24 * H

    def test_same_day_guard_uses_utc_date_of_the_run(self, tmp_path):
        log = NewsletterRunLog(str(tmp_path / "runs.json"))
        log.record(run(created=T0))
        assert log.has_success_on(T0.date())
        assert not log.has_success_on(T0.date() + timedelta(days=1))

    def test_failed_run_does_not_trip_the_guard(self, tmp_path):
        log = NewsletterRunLog(str(tmp_path / "runs.json"))
        log.record(run(status="failed", created=T0, error="beehiiv 500"))
        assert not log.has_success_on(T0.date())

    def test_persists_and_reloads(self, tmp_path):
        path = str(tmp_path / "runs.json")
        NewsletterRunLog(path).record(run(story_count=7, draft_url="https://b/x"))
        reloaded = NewsletterRunLog(path)
        assert reloaded.runs[0]["story_count"] == 7
        assert reloaded.last_successful_window_end() == T0

    def test_write_is_atomic_no_tmp_left_behind(self, tmp_path):
        path = tmp_path / "runs.json"
        NewsletterRunLog(str(path)).record(run())
        assert path.exists() and not path.with_suffix(".json.tmp").exists()

    def test_corrupt_file_starts_fresh(self, tmp_path):
        path = tmp_path / "runs.json"
        path.write_text("{not json")
        log = NewsletterRunLog(str(path))
        assert log.runs == [] and log.last_successful_window_end() is None

    def test_non_list_file_starts_fresh(self, tmp_path):
        path = tmp_path / "runs.json"
        path.write_text(json.dumps({"a": 1}))
        assert NewsletterRunLog(str(path)).runs == []

    def test_naive_timestamps_in_file_are_treated_as_utc(self, tmp_path):
        path = tmp_path / "runs.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "status": "success",
                        "window_start": "2026-09-10T14:30:00",
                        "window_end": "2026-09-11T14:30:00",
                        "created_at": "2026-09-11T14:30:00",
                    }
                ]
            )
        )
        log = NewsletterRunLog(str(path))
        assert log.last_successful_window_end() == T0
        assert log.has_success_on(date(2026, 9, 11))


class TestSentHashes:
    def test_union_of_successful_runs_only(self, tmp_path):
        log = NewsletterRunLog(str(tmp_path / "runs.json"))
        log.record(run(story_hashes=["a", "b"]))
        log.record(run(status="failed", story_hashes=["c"]))
        log.record(run(status="preview", story_hashes=["d"]))
        log.record(run(story_hashes=["b", "e"]))
        assert log.sent_hashes() == {"a", "b", "e"}

    def test_persists_and_tolerates_legacy_rows(self, tmp_path):
        import json

        path = tmp_path / "runs.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "status": "success",
                        "window_start": T0.isoformat(),
                        "window_end": T0.isoformat(),
                        "created_at": T0.isoformat(),
                    }
                ]
            )
        )
        log = NewsletterRunLog(str(path))
        assert log.sent_hashes() == set()  # legacy row has no story_hashes
        log.record(run(story_hashes=["x"]))
        assert NewsletterRunLog(str(path)).sent_hashes() == {"x"}
