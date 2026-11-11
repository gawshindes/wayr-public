"""Newsletter run log and window computation.

The run log is an append-only record of compile runs. The watermark is derived
from it — `last_run = max(window_end) where status == "success"` — rather than
kept as a mutable cursor, which gives the same-day guard, `--force` re-runs and
an audit trail from one structure. **The watermark advances only on success:**
a run that fails to publish records nothing, so the next run re-reads the same
window.

Local JSON is authoritative (data/newsletter_runs.json); Supabase mirrors it.
Prework never writes this file and compile never writes the pool, so the two
Modal functions can run concurrently on one volume without contention.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wayr.utils import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_window(
    now: datetime,
    last_end: Optional[datetime],
    max_story_age_hours: int,
    override_hours: Optional[float] = None,
) -> Tuple[datetime, datetime]:
    """The story window for a compile run starting at `now`.

    - override: `[now - override_hours, now)`, ignoring the watermark. This is
      how a caller asks for more (or less) than the default — the product's
      "include everything I missed" is an override back to the last issue.
    - default: `[last_end, now)`, never reaching further back than
      `max_story_age_hours`. That bound changes no content — the freshness
      filter drops older stories anyway — it just keeps the logged window
      honest. With no successful run yet, the window is simply that bound.

    There is deliberately no separate "period" and no hidden outage clamp:
    cadence belongs to the scheduler and the product, and what to do after a
    missed schedule is the user's choice (see WAYROrchestrator.plan_newsletter).

    `now` is captured once by the caller and used as the window end, so stories
    landing while the run executes fall into the *next* window.
    """
    if override_hours is not None:
        return now - timedelta(hours=override_hours), now

    floor = now - timedelta(hours=max_story_age_hours)
    if last_end is None:
        return floor, now
    return min(max(last_end, floor), now), now


@dataclass
class NewsletterRun:
    window_start: str
    window_end: str
    status: str  # "success" | "failed" | "empty" | "preview"
    story_count: int = 0
    publisher: Optional[str] = None
    external_id: Optional[str] = None
    draft_url: Optional[str] = None
    error: Optional[str] = None
    # What the mailer did after the publisher succeeded (NEWSLETTER_MAILER).
    # A failed send does not fail the run: the issue is on the web and must not
    # ship twice; the error is here for the operator.
    mail_id: Optional[str] = None
    mail_status: Optional[str] = None
    mail_error: Optional[str] = None
    # url_hashes of every story in the issue. Recorded on every run; only
    # successful runs count toward sent_hashes().
    story_hashes: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class NewsletterRunLog:
    """Append-only log of newsletter compile runs."""

    def __init__(self, log_file: str = "data/newsletter_runs.json"):
        self.log_file = Path(log_file)
        self.runs: List[Dict[str, Any]] = []
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---- persistence ----

    def _load(self) -> None:
        if not self.log_file.exists():
            return
        try:
            data = json.loads(self.log_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.runs = [r for r in data if isinstance(r, dict)]
            else:
                logger.warning(f"{self.log_file}: not a list, starting fresh")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"{self.log_file}: unreadable ({e}), starting fresh")
            self.runs = []

    def _save(self) -> None:
        tmp = self.log_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.runs, indent=2), encoding="utf-8")
        os.replace(tmp, self.log_file)

    # ---- writes ----

    def record(self, run: NewsletterRun) -> None:
        self.runs.append(run.to_dict())
        self._save()
        logger.info(
            f"Newsletter run recorded: {run.status}, {run.story_count} stories, "
            f"window {run.window_start} → {run.window_end}"
        )

    # ---- reads ----

    def successes(self) -> List[Dict[str, Any]]:
        return [r for r in self.runs if r.get("status") == "success"]

    def last_successful_window_end(self) -> Optional[datetime]:
        """The watermark."""
        ends = [r["window_end"] for r in self.successes() if r.get("window_end")]
        return _parse(max(ends)) if ends else None

    def has_success_on(self, day: date) -> bool:
        """Same-day guard: did an issue already go out on this UTC date?"""
        for r in self.successes():
            created = r.get("created_at")
            if created and _parse(created).date() == day:
                return True
        return False

    def sent_hashes(self) -> set:
        """Every story that has shipped in a successful issue.

        The watermark stops a story being re-picked only while windows do not
        overlap; --window-hours and --force make overlap routine. This makes
        "never send a story twice" a per-story guarantee instead.
        """
        sent: set = set()
        for r in self.successes():
            sent.update(h for h in (r.get("story_hashes") or []) if h)
        return sent

    def stats(self) -> Dict[str, Any]:
        last = self.last_successful_window_end()
        return {
            "runs": len(self.runs),
            "successes": len(self.successes()),
            "last_successful_window_end": last.isoformat() if last else None,
        }
