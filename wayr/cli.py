"""Command-line interface for WAYR platform."""

import asyncio
import csv
import sys
from pathlib import Path
from typing import Optional
import argparse
from rich.console import Console
from rich.table import Table
from wayr.orchestrator import WAYROrchestrator
from wayr.config import get_settings
from wayr.utils import setup_logging

console = Console()


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser for CLI."""
    parser = argparse.ArgumentParser(
        description="WAYR - AI-powered tech news aggregation platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  wayr run                  # Run the full pipeline
  wayr preview              # Preview without publishing
  wayr test                 # Test connections
  wayr config               # Show current configuration
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run the WAYR pipeline")
    run_parser.add_argument(
        "--skip-publishing",
        action="store_true",
        help="Skip publishing step (preview mode)",
    )
    run_parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file to load configuration from (default: .env)",
    )

    # Prework command (newsletter mode's 2-hourly job)
    prework_parser = subparsers.add_parser(
        "prework",
        help="Newsletter prework: discover and classify into the pool (no authoring, no publishing)",
    )
    prework_parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file to load configuration from (default: .env)",
    )

    # Newsletter command (newsletter mode's compile job)
    nl_parser = subparsers.add_parser(
        "newsletter",
        help="Compile the newsletter window and push a draft to the configured publisher",
    )
    nl_parser.add_argument(
        "--env-file", default=None, help="Path to .env file (default: .env)"
    )
    nl_parser.add_argument(
        "--with-prework",
        action="store_true",
        help="Run prework first, then compile — one command for a demo, a first issue, or recovery",
    )
    nl_parser.add_argument(
        "--skip-publishing",
        action="store_true",
        help="Preview only: write the HTML to data/, push nothing, record nothing",
    )
    nl_parser.add_argument(
        "--dump-candidates",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help="Write the window's scored candidates to CSV (default: data/newsletter_candidates_<date>.csv); implies --skip-publishing",
    )
    nl_parser.add_argument(
        "--window-hours",
        type=float,
        default=None,
        metavar="N",
        help="Use the last N hours as the window, ignoring the watermark and the same-day guard; also lifts the freshness cap to N. Stories already shipped are still excluded",
    )
    nl_parser.add_argument(
        "--plan",
        action="store_true",
        help="Preflight only: show what the usual window and the since-last-issue window would each yield; runs nothing",
    )
    nl_parser.add_argument(
        "--include-sent",
        action="store_true",
        help="Also consider stories that already shipped in an earlier successful issue (inspection)",
    )
    nl_parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the same-day guard and re-run the last window (advances normally on success)",
    )

    # Onboard command (a new newsletter folder from a few answers)
    ob_parser = subparsers.add_parser(
        "onboard",
        help="Create newsletters/<slug>/ from a few answers, starting from the demo closest in shape",
    )
    ob_parser.add_argument(
        "slug", help="folder name: lowercase, digits, dashes (e.g. fintech-weekly)"
    )
    ob_parser.add_argument(
        "--from",
        dest="from_demo",
        default="ai-daily",
        help="demo to start from (default ai-daily)",
    )
    ob_parser.add_argument(
        "--answers",
        help="JSON file with the answers (what a form would post); missing keys are prompted",
    )
    ob_parser.add_argument("--name")
    ob_parser.add_argument("--description")
    ob_parser.add_argument("--readers")
    ob_parser.add_argument("--cadence", choices=("weekdays", "daily", "weekly"))
    ob_parser.add_argument(
        "--sources", help="comma-separated feed URLs, or a path to a sources file"
    )
    ob_parser.add_argument("--industry")
    ob_parser.add_argument("--category-name", dest="category_name")
    ob_parser.add_argument(
        "--overwrite", action="store_true", help="replace an existing folder"
    )
    ob_parser.add_argument(
        "--yes", action="store_true", help="no prompts; missing answers stay blank"
    )

    # Import-pool command (seed the local pool from Supabase)
    ip_parser = subparsers.add_parser(
        "import-pool",
        help="Seed the local story pool from Supabase pipeline_articles (newsletter mode over an existing deployment)",
    )
    ip_parser.add_argument(
        "--days", type=int, default=7, help="How far back to import (default 7)"
    )
    ip_parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="Also import stories rejected at the industry/news stages (feeds the dedup caches)",
    )
    ip_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace entries that already exist locally",
    )
    ip_parser.add_argument(
        "--env-file", default=None, help="Path to .env file (default: .env)"
    )

    # Check-sources command (do the configured feeds deliver stories?)
    cs_parser = subparsers.add_parser(
        "check-sources",
        help="Fetch every configured feed and extract its newest items the way discovery does; report ok / blocked / stale / empty per feed",
    )
    cs_parser.add_argument(
        "--sample",
        type=int,
        default=3,
        help="Newest items to extract per feed (default 3)",
    )
    cs_parser.add_argument(
        "--stale-days",
        type=float,
        default=14,
        help="Flag a feed whose newest item is older than this (default 14)",
    )
    cs_parser.add_argument(
        "--env-file", default=None, help="Path to .env file (default: .env)"
    )

    # Reset command
    reset_parser = subparsers.add_parser(
        "reset", help="Delete local state for a clean cold start"
    )
    reset_parser.add_argument(
        "--newsletter",
        action="store_true",
        help="Delete the feed ledger, the story pool (processed_articles.json) and the newsletter run log",
    )
    reset_parser.add_argument(
        "--yes", "-y", action="store_true", help="Do not ask for confirmation"
    )
    reset_parser.add_argument(
        "--env-file", default=None, help="Path to .env file (default: .env)"
    )

    # Preview command
    preview_parser = subparsers.add_parser(
        "preview", help="Preview pipeline without publishing"
    )
    preview_parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file to load configuration from (default: .env)",
    )

    # Test command
    test_parser = subparsers.add_parser(
        "test",
        help="Test connections (WordPress in news mode, the newsletter publisher in newsletter mode, model keys)",
    )
    test_parser.add_argument(
        "--env-file", default=None, help="Path to .env file (default: .env)"
    )

    # Config command
    config_parser = subparsers.add_parser("config", help="Show current configuration")
    config_parser.add_argument(
        "--env-file", default=None, help="Path to .env file (default: .env)"
    )

    # Eval-classification command
    eval_parser = subparsers.add_parser(
        "eval-classification", help="Run classification evaluation on test dataset"
    )
    eval_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to classification test dataset JSON file",
    )
    eval_parser.add_argument(
        "--model",
        default=None,
        help="Override classification model (format: provider:model_name, e.g. google:gemini-2.5-flash)",
    )

    # Eval-author command
    eval_author_parser = subparsers.add_parser(
        "eval-author", help="Run author agent evaluation"
    )
    eval_author_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to author test dataset JSON file",
    )
    eval_author_parser.add_argument(
        "--model",
        default=None,
        help="Override author model (format: provider:model_name)",
    )

    # Eval-proofreader command
    eval_proof_parser = subparsers.add_parser(
        "eval-proofreader", help="Run proofreader agent evaluation"
    )
    eval_proof_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to proofreader test dataset JSON file",
    )
    eval_proof_parser.add_argument(
        "--model",
        default=None,
        help="Override proofreader model (format: provider:model_name)",
    )

    # Eval-summary command
    eval_summary_parser = subparsers.add_parser(
        "eval-summary", help="Run summary agent evaluation"
    )
    eval_summary_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to summary test dataset JSON file",
    )
    eval_summary_parser.add_argument(
        "--model",
        default=None,
        help="Override summary model (format: provider:model_name)",
    )

    # Eval-industry command
    eval_industry_parser = subparsers.add_parser(
        "eval-industry", help="Run industry classifier evaluation"
    )
    eval_industry_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to classification test dataset JSON file",
    )
    eval_industry_parser.add_argument(
        "--model",
        default=None,
        help="Override industry classifier model (format: provider:model_name)",
    )

    # Eval-news-type command
    eval_news_type_parser = subparsers.add_parser(
        "eval-news-type", help="Run news classifier evaluation"
    )
    eval_news_type_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to classification test dataset JSON file",
    )
    eval_news_type_parser.add_argument(
        "--model",
        default=None,
        help="Override news classifier model (format: provider:model_name)",
    )

    # Eval-category command
    eval_category_parser = subparsers.add_parser(
        "eval-category", help="Run category classifier evaluation"
    )
    eval_category_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to classification test dataset JSON file",
    )
    eval_category_parser.add_argument(
        "--model",
        default=None,
        help="Override category classifier model (format: provider:model_name)",
    )

    # Eval-dedup command
    eval_dedup_parser = subparsers.add_parser(
        "eval-dedup", help="Run deduplication agent evaluation"
    )
    eval_dedup_parser.add_argument(
        "--dataset",
        default=None,
        help="Path to dedup test dataset JSON file",
    )
    eval_dedup_parser.add_argument(
        "--model",
        default=None,
        help="Override dedup model (format: provider:model_name)",
    )

    # Eval-all command
    eval_all_parser = subparsers.add_parser(
        "eval-all", help="Run all agent evaluations"
    )
    eval_all_parser.add_argument(
        "--skip-expensive",
        action="store_true",
        help="Skip author and proofreader evals (higher LLM cost)",
    )

    # Show-excluded command
    excluded_parser = subparsers.add_parser(
        "show-excluded", help="Show articles excluded as non-news"
    )
    excluded_parser.add_argument(
        "--type",
        choices=["opinion", "review", "guide", "deal", "other"],
        help="Filter by content type",
    )
    excluded_parser.add_argument(
        "--limit", type=int, default=50, help="Number of articles to show (default: 50)"
    )

    return parser


async def cmd_run(skip_publishing: bool = False, env_file: Optional[str] = None):
    """Run the WAYR pipeline."""
    try:
        if env_file:
            get_settings(env_file=env_file)
            console.print(f"[dim]Loaded configuration from {env_file}[/dim]")
        orchestrator = WAYROrchestrator()
        results = await orchestrator.run_pipeline(skip_publishing=skip_publishing)

        # Display summary
        console.print("\n[bold green]Pipeline Summary:[/bold green]")
        console.print(f"  Discovered: {len(results['discovered'])} articles")
        console.print(f"  Classified: {len(results['classified'])} articles")
        console.print(f"  Generated: {len(results['generated'])} articles")
        console.print(f"  Published: {len(results['published'])} articles")

        if results["errors"]:
            console.print(f"\n[bold red]Errors: {len(results['errors'])}[/bold red]")
            for error in results["errors"]:
                console.print(f"  - {error}")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


async def cmd_prework(env_file: Optional[str] = None):
    """Run newsletter prework: research only, no authoring or publishing."""
    try:
        if env_file:
            get_settings(env_file=env_file)
            console.print(f"[dim]Loaded configuration from {env_file}[/dim]")
        orchestrator = WAYROrchestrator()
        results = await orchestrator.run_prework()

        _print_spend(orchestrator)
        console.print("\n[bold green]Prework Summary:[/bold green]")
        console.print(f"  Discovered: {len(results['discovered'])} articles")
        console.print(f"  Classified: {len(results['classified'])} articles")
        console.print(f"  News: {len(results['news'])} articles")
        ledger = results.get("ledger") or {}
        if ledger:
            console.print(
                f"  Ledger: {ledger.get('items')} items across {ledger.get('feeds')} feeds, "
                f"{ledger.get('unadmitted')} not yet admitted"
            )
            if ledger.get("failing_feeds"):
                console.print(
                    f"  [yellow]Failing feeds: {', '.join(ledger['failing_feeds'])}[/yellow]"
                )

        if results["errors"]:
            console.print(f"\n[bold red]Errors: {len(results['errors'])}[/bold red]")
            for error in results["errors"]:
                console.print(f"  - {error}")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def _data_dir(settings) -> Path:
    return Path(settings.newsletter_runs_file).parent


def _write_preview(settings, results: dict) -> Path:
    d = _data_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"newsletter_preview_{results['window_end'][:10]}.html"
    path.write_text(results["html"], encoding="utf-8")
    return path


def _write_candidates_csv(settings, results: dict, path: str) -> Path:
    """The calibration tool: every candidate in the window with its score and tier.

    This is how the two thresholds get set — they cannot be picked without
    seeing the score distribution over real feed data.
    """
    tier_of: dict = {}
    for tier in ("featured", "roundup", "dropped", "unscored"):
        for d in results.get(tier) or []:
            tier_of[d.get("url_hash")] = tier
    out = (
        Path(path)
        if path
        else _data_dir(settings)
        / f"newsletter_candidates_{results['window_end'][:10]}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    def score(d: dict) -> float:
        v = (d.get("importance") or {}).get("score")
        return float(v) if isinstance(v, (int, float)) else -1.0

    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "tier",
                "score",
                "reasoning",
                "title",
                "source",
                "category",
                "url",
                "eligible_at",
                "rss_published_at",
                "url_hash",
            ]
        )
        for d in sorted(results.get("candidates") or [], key=score, reverse=True):
            imp = d.get("importance") or {}
            w.writerow(
                [
                    tier_of.get(d.get("url_hash"), "?"),
                    imp.get("score", ""),
                    imp.get("reasoning", ""),
                    d.get("title", ""),
                    d.get("source", ""),
                    (d.get("category") or {}).get("primary", ""),
                    d.get("url", ""),
                    d.get("eligible_at", ""),
                    d.get("rss_published_at", ""),
                    d.get("url_hash", ""),
                ]
            )
    return out


async def cmd_newsletter(
    env_file: Optional[str] = None,
    with_prework: bool = False,
    skip_publishing: bool = False,
    force: bool = False,
    window_hours: Optional[float] = None,
    dump_candidates: Optional[str] = None,
    include_sent: bool = False,
    plan: bool = False,
):
    """Compile the newsletter window; push a draft unless previewing."""
    try:
        if env_file:
            get_settings(env_file=env_file)
            console.print(f"[dim]Loaded configuration from {env_file}[/dim]")
        settings = get_settings()
        orchestrator = WAYROrchestrator()

        if plan:
            p = orchestrator.plan_newsletter()
            console.print(
                "\n[bold green]Newsletter Plan[/bold green] (nothing run, nothing recorded)"
            )
            last = p["last_successful_at"] or "never"
            console.print(
                f"  Last successful issue: {last}"
                + (
                    f"  ({p['hours_since_last']}h ago)"
                    if p["hours_since_last"] is not None
                    else ""
                )
            )
            if p["missed_schedule"]:
                console.print(
                    f"  [yellow]Missed schedule:[/yellow] more than {p['max_story_age_hours']}h since the last issue — a default run would drop older stories"
                )
            if p["sent_today"]:
                console.print(
                    "  An issue already went out today (UTC); a default run needs --force"
                )
            u = p["usual"]
            console.print(
                f"  Usual ({u['window_hours']:.0f}h):            {u['candidates']} candidates, {u['already_sent']} already shipped, {u['unscored']} unscored"
            )
            d = p["since_last_issue"]
            if d:
                console.print(
                    f"  Since last issue ({d['window_hours']:.0f}h): {d['candidates']} candidates, {d['already_sent']} already shipped, {d['unscored']} unscored  →  --window-hours {d['window_hours']:.0f}"
                )
            return

        if with_prework:
            console.print("[bold]Prework[/bold] (discover → classify → score)")
            pre = await orchestrator.run_prework()
            console.print(
                f"  Discovered {len(pre['discovered'])}, news {len(pre['news'])}, "
                f"scored {len(pre['scored'])}"
            )

        preview = skip_publishing or dump_candidates is not None
        results = await orchestrator.run_newsletter(
            skip_publishing=preview,
            force=force,
            window_hours=window_hours,
            include_sent=include_sent,
        )
        status = results["status"]

        _print_spend(orchestrator)
        console.print("\n[bold green]Newsletter Summary:[/bold green]")
        console.print(
            f"  Window [{results['window_source']}]: {results['window_start']} → {results['window_end']}"
        )
        console.print(f"  Status: [bold]{status}[/bold]")
        if results.get("already_sent"):
            console.print(
                f"  Already shipped (excluded): {results['already_sent']}  — use --include-sent to see them"
            )

        if dump_candidates is not None:
            if results.get("candidates"):
                path = _write_candidates_csv(settings, results, dump_candidates)
                console.print(
                    f"  Candidates CSV: {path}  ({len(results['candidates'])} rows)"
                )
            else:
                console.print("  Candidates CSV: nothing to dump")

        if status == "already_sent":
            console.print(
                "  An issue already went out today (UTC). Use --force to re-run, "
                "or --window-hours N to inspect a window."
            )
            return
        if status == "empty":
            console.print("  No candidates in the window.")
            return

        c = results.get("counts") or {}
        console.print(
            f"  Selected: {c.get('featured', 0)} featured, {c.get('roundup', 0)} roundup, "
            f"{c.get('dropped', 0)} dropped, {c.get('unscored', 0)} unscored"
        )
        for sec in results.get("sections") or []:
            kind = "placeholder" if sec["placeholder"] else sec["style"]
            console.print(f"    - {sec['name']} [{kind}]: {sec['stories']}")
        if results.get("unplaced"):
            console.print(
                f"  [yellow]Unplaced: {len(results['unplaced'])} — add a catch-all section[/yellow]"
            )

        comp = results.get("compile") or {}
        console.print(
            f"  Compile: {'LLM' if comp.get('llm_generated') else 'deterministic fallback'}, "
            f"{comp.get('summaries', 0)} blurbs"
            + (
                f", compile error: {comp['compile_error']}"
                if comp.get("compile_error")
                else ""
            )
        )

        if status == "preview":
            path = _write_preview(settings, results)
            console.print(f"  Preview HTML: {path}")
            return
        if status == "published":
            console.print(
                f"  [bold green]Issue is in {results['publisher']}[/bold green]: "
                f"id {results['external_id']}"
                + (f", {results['draft_url']}" if results.get("draft_url") else "")
            )
            mail = results.get("mail")
            if mail:
                console.print(
                    f"  [bold green]Email {mail['status']} via {mail['mailer']}[/bold green]: "
                    f"id {mail['external_id']}"
                    + (f", {mail['url']}" if mail.get("url") else "")
                )
            social = results.get("social")
            if social:
                posted = [p for p, r in social.items() if r.get("posted")]
                if posted:
                    console.print(
                        f"  [bold green]Shared on {', '.join(posted)}[/bold green]"
                    )
            for err in results.get("errors") or []:
                console.print(f"  [bold red]{err}[/bold red]")
            if results.get("errors"):
                sys.exit(1)
            return
        if status == "failed":
            console.print("\n[bold red]Draft push failed:[/bold red]")
            for err in results.get("errors") or []:
                console.print(f"  - {err}")
            sys.exit(1)

    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def _print_spend(orchestrator) -> None:
    """One line per agent that made calls: tokens and dollars for this run."""
    rows = orchestrator.usage_report()
    if not rows:
        return
    console.print("\n[bold]Spend this run:[/bold]")
    total = 0.0
    priced = True
    for r in rows:
        cost = r["cost_usd"]
        if cost is None:
            priced = False
        else:
            total += cost
        console.print(
            f"  {r['agent']:<24} {r['model']:<24} {r['calls']:>4} calls  "
            f"{r['input_tokens']:>8,} in  {r['output_tokens']:>7,} out "
            f"({r['reasoning_tokens']:,} reasoning)  "
            + (f"${cost:.4f}" if cost is not None else "price unknown")
        )
    console.print(
        "  total"
        + " " * 61
        + (f"${total:.4f}" if priced else f"≥ ${total:.4f} (some models unpriced)")
    )


def cmd_check_sources(
    env_file: Optional[str] = None, sample: int = 3, stale_days: float = 14
) -> int:
    """Report, per configured feed, whether its stories would reach the classifiers.

    Fetches each feed and extracts its newest `sample` items exactly as
    discovery would, so a paywalled or bot-protected outlet shows up as
    `blocked` here instead of as silent zero-word drops in a prework run.
    Returns a process exit code: 1 if any feed is blocked or empty.
    """
    from wayr.services import ContentExtractor, RSSFetcher
    from wayr.services.source_check import check_sources, skipped_report, summarize

    if env_file:
        get_settings(env_file=env_file)
        console.print(f"[dim]Loaded configuration from {env_file}[/dim]")
    settings = get_settings()
    sources = settings.sources
    rss = [s for s in sources if s.type == "rss"]
    console.print(
        f"\n[bold]Checking {len(sources)} source(s)[/bold] — {sample} newest items each, "
        f"readable = ≥{settings.min_content_words} words\n"
    )

    fetcher = RSSFetcher(rss)  # no ledger: a check never records anything
    extractor = ContentExtractor()
    checked = asyncio.run(
        check_sources(
            [s.url for s in rss],
            fetcher.fetch_feed,
            extractor.extract,
            sample=sample,
            min_words=settings.min_content_words,
            stale_after_hours=stale_days * 24,
        )
    )
    by_url = {r.url: r for r in checked}
    for s in rss:
        by_url[s.url].origin = s.origin
    # Back in file order, page sources shown as skipped where they sit.
    reports = [
        by_url[s.url] if s.type == "rss" else skipped_report(s.url, s.origin)
        for s in sources
    ]

    table = Table(show_lines=False)
    table.add_column("verdict", style="bold")
    table.add_column("origin")
    table.add_column("items", justify="right")
    table.add_column("newest", justify="right")
    table.add_column("readable", justify="right")
    table.add_column("words (sampled)")
    table.add_column("feed")
    colour = {
        "ok": "green",
        "blocked": "red",
        "stale": "yellow",
        "empty": "red",
        "skipped": "dim",
    }
    for r in reports:
        newest = (
            "—"
            if r.newest_hours is None
            else (
                f"{r.newest_hours:.0f}h"
                if r.newest_hours < 72
                else f"{r.newest_hours / 24:.0f}d"
            )
        )
        table.add_row(
            f"[{colour[r.verdict]}]{r.verdict}[/{colour[r.verdict]}]",
            r.origin,
            str(r.items),
            newest,
            f"{r.readable}/{r.sampled}" if r.sampled else "—",
            " ".join(str(w) for w in r.words) if r.words else (r.note or "—"),
            r.url,
        )
    console.print(table)
    counts = summarize(reports)
    console.print(
        f"\n{counts['ok']} ok · {counts['blocked']} blocked · {counts['stale']} stale · "
        f"{counts['empty']} empty · {counts['skipped']} skipped (type=page)"
    )
    bad = [r for r in reports if r.verdict in ("blocked", "empty")]
    if bad:
        console.print(
            "\n[yellow]Blocked/empty feeds cost a fetch and an extraction per item and never yield a story — "
            "comment them out of the sources file.[/yellow]"
        )
    return 1 if bad else 0


def cmd_onboard(args) -> int:
    """Write newsletters/<slug>/ from answers given as flags, a JSON file, or prompts."""
    import json
    from pathlib import Path

    from wayr.onboarding import Answers, build, next_steps, write

    given = {}
    if args.answers:
        given.update(json.loads(Path(args.answers).read_text(encoding="utf-8")))
    for key in (
        "name",
        "description",
        "readers",
        "cadence",
        "industry",
        "category_name",
    ):
        val = getattr(args, key, None)
        if val:
            given[key] = val
    if args.sources:
        p = Path(args.sources)
        given["sources"] = (
            [ln for ln in p.read_text(encoding="utf-8").splitlines()]
            if p.is_file()
            else [u.strip() for u in args.sources.split(",") if u.strip()]
        )

    def ask(key, prompt, default="", multiline=False):
        if key in given and given[key] not in (None, ""):
            return given[key]
        if args.yes:
            return default
        if multiline:
            console.print(f"[bold]{prompt}[/bold] (one per line; empty line to finish)")
            lines = []
            while True:  # bounded-loop-ok (ends on an empty line from the user)
                ln = console.input("  > ").strip()
                if not ln:
                    break
                lines.append(ln)
            return lines
        shown = f" [dim]({default})[/dim]" if default else ""
        val = console.input(f"[bold]{prompt}[/bold]{shown}: ").strip()
        return val or default

    console.print(
        f"\n[bold]Onboarding [cyan]{args.slug}[/cyan] from the [cyan]{args.from_demo}[/cyan] demo[/bold]\n"
    )
    a = Answers(
        slug=args.slug,
        name=ask(
            "name", "Newsletter name", default=args.slug.replace("-", " ").title()
        ),
        description=ask("description", "One line on what it is"),
        readers=ask("readers", "Who reads it (a sentence or two)"),
        cadence=ask(
            "cadence", "Cadence (weekdays / daily / weekly)", default="weekdays"
        ),
        sources=ask("sources", "Feed URLs", multiline=True) or [],
        matters=ask("matters", "What matters most to these readers", multiline=True)
        or [],
        avoid=ask("avoid", "What matters less", multiline=True) or [],
        industry=given.get("industry"),
        categories=given.get("categories"),
        category_name=given.get("category_name"),
        voice_notes=given.get("voice_notes", ""),
    )
    try:
        files = build(a, args.from_demo)
        dest = write(files, a.slug, overwrite=args.overwrite)
    except (ValueError, FileNotFoundError, FileExistsError) as e:
        console.print(f"[bold red]{e}[/bold red]")
        return 1
    console.print(f"\n[green]Wrote[/green] {dest}/ — {', '.join(files)}")
    console.print("Review the five files, then:\n" + next_steps(a.slug))
    return 0


def cmd_import_pool(
    env_file: Optional[str] = None,
    days: int = 7,
    include_rejected: bool = False,
    overwrite: bool = False,
):
    """Seed ARTICLE_CACHE_FILE from Supabase pipeline_articles.

    This is how newsletter mode runs over an existing news deployment without
    Modal: the rows carry raw_content, so blurbs need no re-extraction, and any
    missing importance scores are filled lazily by the compile job.
    """
    from wayr.services import ArticleTracker, SupabasePipelineStore
    from wayr.services.article_tracker import entry_from_pipeline_row

    if env_file:
        get_settings(env_file=env_file)
        console.print(f"[dim]Loaded configuration from {env_file}[/dim]")
    settings = get_settings()

    store = SupabasePipelineStore(
        url=settings.supabase_url,
        secret_key=settings.supabase_secret_key,
        region=settings.region,
    )
    if not store.enabled:
        console.print(
            "[bold red]SUPABASE_URL and SUPABASE_SECRET_KEY are required for import-pool.[/bold red]"
        )
        sys.exit(1)

    stages = (
        ("industry", "news", "category", "published")
        if include_rejected
        else ("category", "published")
    )
    rows = store.fetch_pool(days=days, stages=stages)
    entries: dict = {}
    skipped = 0
    for row in rows:
        mapped = entry_from_pipeline_row(row)
        if mapped is None:
            skipped += 1
            continue
        h, entry = mapped
        entries[h] = entry

    tracker = ArticleTracker(
        cache_file=settings.article_cache_file,
        retention_days=settings.article_retention_days,
    )
    written = tracker.import_entries(entries, overwrite=overwrite)

    by_stage: dict = {}
    for e in entries.values():
        by_stage[e["last_stage"]] = by_stage.get(e["last_stage"], 0) + 1
    console.print("\n[bold green]Import Summary:[/bold green]")
    console.print(
        f"  Fetched: {len(rows)} rows (last {days}d, region {settings.region or 'any'})"
    )
    console.print(
        f"  Written: {written}  Already present: {len(entries) - written}  Unmappable: {skipped}"
    )
    console.print(f"  By stage: {by_stage}")
    console.print(
        f"  With content: {sum(1 for e in entries.values() if e.get('content'))}  "
        f"Already scored: {sum(1 for e in entries.values() if e.get('importance'))}"
    )
    console.print(f"  Pool file: {settings.article_cache_file}")
    if not entries:
        console.print("  [yellow]Nothing to import — check REGION and --days.[/yellow]")


def cmd_reset(
    env_file: Optional[str] = None, newsletter: bool = False, yes: bool = False
):
    """Delete local state files for a clean cold start."""
    if env_file:
        get_settings(env_file=env_file)
    settings = get_settings()
    if not newsletter:
        console.print("Specify what to reset: --newsletter")
        sys.exit(2)

    targets = [
        Path(settings.feed_ledger_file),
        Path(settings.article_cache_file),
        Path(settings.newsletter_runs_file),
    ]
    existing = [t for t in targets if t.exists()]
    if not existing:
        console.print("Nothing to reset — none of the state files exist.")
        return

    console.print("[bold]This will delete:[/bold]")
    for t in existing:
        console.print(f"  - {t}")
    console.print(
        "[dim]processed_articles.json is the story pool; in a news-mode deployment it is also "
        "the per-story dedup cache.[/dim]"
    )
    if not yes:
        if not sys.stdin.isatty():
            console.print(
                "[red]Refusing to delete without --yes in a non-interactive session.[/red]"
            )
            sys.exit(2)
        if input("Delete these files? [y/N] ").strip().lower() != "y":
            console.print("Aborted.")
            return
    for t in existing:
        t.unlink()
    console.print(
        f"[green]Deleted {len(existing)} file(s). Next run is a cold start.[/green]"
    )


async def cmd_preview(env_file: Optional[str] = None):
    """Preview pipeline without publishing."""
    await cmd_run(skip_publishing=True, env_file=env_file)


async def cmd_test(env_file: Optional[str] = None):
    """Test connections."""
    try:
        if env_file:
            get_settings(env_file=env_file)
            console.print(f"[dim]Loaded configuration from {env_file}[/dim]")
        console.print("[bold]Testing WAYR Connections...[/bold]\n")
        orchestrator = WAYROrchestrator()
        success = await orchestrator.test_connections()

        if success:
            console.print("\n[bold green]✓ All connections successful![/bold green]")
            sys.exit(0)
        else:
            console.print("\n[bold red]✗ Connection test failed[/bold red]")
            sys.exit(1)

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


def cmd_config(env_file: Optional[str] = None):
    """Show current configuration."""
    try:
        if env_file:
            get_settings(env_file=env_file)
        settings = get_settings()

        table = Table(title="WAYR Configuration", show_header=True)
        table.add_column("Setting", style="cyan", no_wrap=True)
        table.add_column("Value", style="magenta")

        # Display non-sensitive settings
        table.add_row("Platform Name", settings.platform_name)
        table.add_row("Pipeline Mode", settings.pipeline_mode)
        if settings.pipeline_mode == "news":
            table.add_row("WordPress URL", str(settings.wordpress_url))
            table.add_row("WordPress Username", settings.wordpress_username or "")
        else:
            table.add_row("Beehiiv Publication", settings.beehiiv_publication_id or "")
        table.add_row("OpenAI Model", settings.openai_model)
        table.add_row("RSS Feeds", str(len(settings.rss_feeds)))
        table.add_row(
            "Max Articles Per Run", str(settings.max_articles_processed_per_run)
        )
        table.add_row("Article Type", settings.article_type)
        table.add_row(
            "Article Target Words",
            str(settings.get_target_words()),
        )
        table.add_row("Industry", settings.industry)
        table.add_row("Log Level", settings.log_level)

        # Pipeline stage toggles
        stage_toggles = [
            ("Discovery", settings.enable_discovery),
            ("Classification", settings.enable_classification),
            ("Author", settings.enable_author),
            ("Proofreading", settings.enable_proofreading),
            ("Summary", settings.enable_summary),
            ("Publishing", settings.enable_publishing),
            ("Social Posting", settings.enable_social_posting),
        ]
        for name, enabled in stage_toggles:
            table.add_row(
                f"Stage: {name}",
                "[green]enabled[/green]" if enabled else "[red]disabled[/red]",
            )

        console.print(table)

        # List RSS feeds
        console.print("\n[bold]RSS Feeds:[/bold]")
        for i, feed in enumerate(settings.rss_feeds, 1):
            console.print(f"  {i}. {feed}")

    except Exception as e:
        console.print(f"[bold red]Error loading configuration:[/bold red] {e}")
        console.print("\nMake sure you have a .env file with required settings.")
        sys.exit(1)


async def cmd_eval_classification(dataset: str | None = None, model: str | None = None):
    """Run classification evaluation."""
    try:
        from wayr.evaluation import ClassificationEvaluator
        from wayr.evaluation.classification.eval import DEFAULT_TEST_CASES

        dataset = dataset or str(DEFAULT_TEST_CASES)

        console.print("[bold]Running Classification Evaluation...[/bold]\n")

        settings = get_settings()

        # When --model is passed, use it for all three agents; otherwise
        # resolve each agent's model individually (they can differ).
        if model:
            cfg_industry = cfg_news = cfg_category = settings.get_model_config(model)
        else:
            cfg_industry = settings.get_classification_model("industry")
            cfg_news = settings.get_classification_model("news")
            cfg_category = settings.get_classification_model("category")

        industry_agent, news_agent, category_agent = _build_classification_agents(
            settings, cfg_industry, cfg_news, cfg_category
        )

        console.print(
            f"[dim]Industry classifier: {cfg_industry['provider']}:{cfg_industry['model_name']}[/dim]"
        )
        console.print(
            f"[dim]News classifier: {cfg_news['provider']}:{cfg_news['model_name']}[/dim]"
        )
        console.print(
            f"[dim]Category classifier: {cfg_category['provider']}:{cfg_category['model_name']}[/dim]\n"
        )

        evaluator = ClassificationEvaluator(
            industry_classifier_agent=industry_agent,
            news_classifier_agent=news_agent,
            category_classifier_agent=category_agent,
        )

        # Run evaluation
        result = await evaluator.run_test_suite(dataset)

        # Print report
        console.print(result.generate_report())

        # Exit with appropriate code based on L1 (content type) accuracy
        # L1 is CRITICAL - must be >= 95% for production
        # L2 is nice-to-have - just for WordPress organization
        l1_threshold = 0.95  # 95% for L1 (content type)

        if result.l1_pass_rate >= l1_threshold:
            console.print("\n[bold green]✓ Evaluation PASSED[/bold green]")
            console.print(
                f"L1 (Content Type): {result.l1_pass_rate:.1%} >= {l1_threshold:.0%} threshold"
            )
            if result.l2_pass_rate < 0.9:
                console.print(
                    f"[yellow]Note: L2 (Category) at {result.l2_pass_rate:.1%} - WordPress categories may need tuning[/yellow]"
                )
            sys.exit(0)
        else:
            console.print("\n[bold red]✗ Evaluation FAILED[/bold red]")
            console.print(
                f"L1 (Content Type): {result.l1_pass_rate:.1%} < {l1_threshold:.0%} threshold (CRITICAL)"
            )
            sys.exit(1)

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


def _build_industry_classifier(settings, cfg):
    from wayr.agents.industry_classifier import IndustryClassifierAgent

    return IndustryClassifierAgent(
        api_key=cfg["api_key"],
        model=cfg["model_name"],
        provider=cfg["provider"],
        temperature=settings.classification_temperature,
        max_tokens=settings.classification_max_tokens,
        industry=settings.industry,
    )


def _build_news_classifier(settings, cfg):
    from wayr.agents.news_classifier import NewsClassifierAgent

    return NewsClassifierAgent(
        api_key=cfg["api_key"],
        model=cfg["model_name"],
        provider=cfg["provider"],
        temperature=settings.classification_temperature,
        max_tokens=settings.classification_max_tokens,
    )


def _build_category_classifier(settings, cfg):
    from wayr.agents.category_classifier import CategoryClassifierAgent

    return CategoryClassifierAgent(
        api_key=cfg["api_key"],
        model=cfg["model_name"],
        provider=cfg["provider"],
        temperature=settings.classification_temperature,
        max_tokens=settings.classification_max_tokens,
        industry=settings.industry,
        allowed_categories=settings.industry_categories,
    )


def _build_classification_agents(settings, cfg_industry, cfg_news, cfg_category):
    """Construct the three classification agents from resolved model configs."""
    return (
        _build_industry_classifier(settings, cfg_industry),
        _build_news_classifier(settings, cfg_news),
        _build_category_classifier(settings, cfg_category),
    )


def _create_agent(
    agent_class, model_override: Optional[str], settings_model_attr: str, **extra_kwargs
):
    """Helper to create an agent with optional model override."""
    settings = get_settings()
    model_spec = (
        model_override if model_override else getattr(settings, settings_model_attr)
    )
    config = settings.get_model_config(model_spec)
    return (
        agent_class(
            api_key=config["api_key"],
            model=config["model_name"],
            provider=config["provider"],
            **extra_kwargs,
        ),
        config,
    )


async def cmd_eval_industry_classifier(
    dataset: str | None = None, model: str | None = None
):
    """Run the industry classifier evaluation in isolation."""
    try:
        from wayr.evaluation import IndustryClassifierEvaluator
        from wayr.evaluation.industry_classifier.eval import DEFAULT_TEST_CASES

        dataset = dataset or str(DEFAULT_TEST_CASES)

        console.print("[bold]Running Industry Classifier Evaluation...[/bold]\n")
        settings = get_settings()
        cfg = (
            settings.get_model_config(model)
            if model
            else settings.get_classification_model("industry")
        )
        agent = _build_industry_classifier(settings, cfg)
        console.print(
            f"[dim]Using model: {cfg['provider']}:{cfg['model_name']}[/dim]\n"
        )
        evaluator = IndustryClassifierEvaluator(agent)
        result = await evaluator.run_test_suite(dataset)
        console.print(result.generate_report())
        return result.pass_rate >= 0.85
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        import traceback

        traceback.print_exc()
        return False


async def cmd_eval_news_classifier(
    dataset: str | None = None, model: str | None = None
):
    """Run the news classifier evaluation in isolation."""
    try:
        from wayr.evaluation import NewsClassifierEvaluator
        from wayr.evaluation.news_classifier.eval import DEFAULT_TEST_CASES

        dataset = dataset or str(DEFAULT_TEST_CASES)

        console.print("[bold]Running News Classifier Evaluation...[/bold]\n")
        settings = get_settings()
        cfg = (
            settings.get_model_config(model)
            if model
            else settings.get_classification_model("news")
        )
        agent = _build_news_classifier(settings, cfg)
        console.print(
            f"[dim]Using model: {cfg['provider']}:{cfg['model_name']}[/dim]\n"
        )
        evaluator = NewsClassifierEvaluator(agent)
        result = await evaluator.run_test_suite(dataset)
        console.print(result.generate_report())
        return result.pass_rate >= 0.85
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        import traceback

        traceback.print_exc()
        return False


async def cmd_eval_category_classifier(
    dataset: str | None = None, model: str | None = None
):
    """Run the category classifier evaluation in isolation."""
    try:
        from wayr.evaluation import CategoryClassifierEvaluator
        from wayr.evaluation.category_classifier.eval import DEFAULT_TEST_CASES

        dataset = dataset or str(DEFAULT_TEST_CASES)

        console.print("[bold]Running Category Classifier Evaluation...[/bold]\n")
        settings = get_settings()
        cfg = (
            settings.get_model_config(model)
            if model
            else settings.get_classification_model("category")
        )
        agent = _build_category_classifier(settings, cfg)
        console.print(
            f"[dim]Using model: {cfg['provider']}:{cfg['model_name']}[/dim]\n"
        )
        evaluator = CategoryClassifierEvaluator(agent)
        result = await evaluator.run_test_suite(dataset)
        console.print(result.generate_report())
        return result.pass_rate >= 0.85
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        import traceback

        traceback.print_exc()
        return False


async def cmd_eval_agent(
    agent_name: str, dataset: str | None = None, model: str | None = None
):
    """Run evaluation for a specific agent."""
    try:
        settings = get_settings()
        console.print(f"[bold]Running {agent_name.title()} Evaluation...[/bold]\n")

        from wayr.evaluation.base_eval import BaseEvaluator

        evaluator: BaseEvaluator
        if agent_name == "author":
            from wayr.agents.author import AuthorAgent
            from wayr.evaluation import AuthorEvaluator
            from wayr.evaluation.author.eval import DEFAULT_TEST_CASES

            dataset = dataset or str(DEFAULT_TEST_CASES)
            agent, config = _create_agent(
                AuthorAgent,
                model,
                "model_author",
                ai_disclosure=settings.ai_disclosure_notice,
                add_source_citation=settings.add_source_citation,
            )
            evaluator = AuthorEvaluator(agent)

        elif agent_name == "proofreader":
            from wayr.agents.proofreader import ProofreaderAgent
            from wayr.evaluation import ProofreaderEvaluator
            from wayr.evaluation.proofreader.eval import DEFAULT_TEST_CASES

            dataset = dataset or str(DEFAULT_TEST_CASES)
            agent, config = _create_agent(
                ProofreaderAgent,
                model,
                "model_proofreader",
                target_words=settings.get_target_words(),
            )
            evaluator = ProofreaderEvaluator(agent)

        elif agent_name == "summary":
            from wayr.agents.summary import SummaryAgent
            from wayr.evaluation import SummaryEvaluator
            from wayr.evaluation.summary.eval import DEFAULT_TEST_CASES

            dataset = dataset or str(DEFAULT_TEST_CASES)
            agent, config = _create_agent(SummaryAgent, model, "model_summary")
            evaluator = SummaryEvaluator(agent)

        elif agent_name == "dedup":
            from wayr.agents.deduplication import DeduplicationAgent
            from wayr.evaluation import DeduplicationEvaluator
            from wayr.evaluation.dedup.eval import DEFAULT_TEST_CASES

            dataset = dataset or str(DEFAULT_TEST_CASES)
            agent, config = _create_agent(DeduplicationAgent, model, "model_dedup")
            evaluator = DeduplicationEvaluator(agent)

        else:
            console.print(f"[bold red]Unknown agent: {agent_name}[/bold red]")
            sys.exit(1)

        console.print(
            f"[dim]Using model: {config['provider']}:{config['model_name']}[/dim]\n"
        )

        result = await evaluator.run_test_suite(dataset)
        console.print(result.generate_report())

        if result.pass_rate >= 0.85:
            console.print(
                f"\n[bold green]✓ {agent_name.title()} evaluation PASSED ({result.pass_rate:.1%})[/bold green]"
            )
            return True
        else:
            console.print(
                f"\n[bold red]✗ {agent_name.title()} evaluation FAILED ({result.pass_rate:.1%})[/bold red]"
            )
            return False

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        import traceback

        traceback.print_exc()
        return False


async def cmd_eval_all(skip_expensive: bool = False):
    """Run all agent evaluations."""
    console.print("[bold]Running All Agent Evaluations...[/bold]\n")

    results = {}

    # Classification
    try:
        console.print("[bold cyan]━━━ Classification ━━━[/bold cyan]")
        settings = get_settings()
        from wayr.evaluation import ClassificationEvaluator

        cfg_industry = settings.get_classification_model("industry")
        cfg_news = settings.get_classification_model("news")
        cfg_category = settings.get_classification_model("category")
        industry_agent, news_agent, category_agent = _build_classification_agents(
            settings, cfg_industry, cfg_news, cfg_category
        )
        evaluator = ClassificationEvaluator(
            industry_classifier_agent=industry_agent,
            news_classifier_agent=news_agent,
            category_classifier_agent=category_agent,
        )
        from wayr.evaluation.classification.eval import (
            DEFAULT_TEST_CASES as _cls_default,
        )

        result = await evaluator.run_test_suite(str(_cls_default))
        console.print(result.generate_report())
        results["classification"] = result.pass_rate
    except Exception as e:
        console.print(f"[red]Classification eval failed: {e}[/red]")
        results["classification"] = 0.0

    # Per-agent classification evals (cheap, non-expensive tier)
    console.print("\n[bold cyan]━━━ Industry Classifier ━━━[/bold cyan]")
    results["industry_classifier"] = await cmd_eval_industry_classifier()

    console.print("\n[bold cyan]━━━ News Classifier ━━━[/bold cyan]")
    results["news_classifier"] = await cmd_eval_news_classifier()

    console.print("\n[bold cyan]━━━ Category Classifier ━━━[/bold cyan]")
    results["category_classifier"] = await cmd_eval_category_classifier()

    # Dedup
    console.print("\n[bold cyan]━━━ Deduplication ━━━[/bold cyan]")
    passed = await cmd_eval_agent("dedup")
    results["dedup"] = passed

    # Summary
    console.print("\n[bold cyan]━━━ Summary ━━━[/bold cyan]")
    passed = await cmd_eval_agent("summary")
    results["summary"] = passed

    if not skip_expensive:
        # Author
        console.print("\n[bold cyan]━━━ Author ━━━[/bold cyan]")
        passed = await cmd_eval_agent("author")
        results["author"] = passed

        # Proofreader
        console.print("\n[bold cyan]━━━ Proofreader ━━━[/bold cyan]")
        passed = await cmd_eval_agent("proofreader")
        results["proofreader"] = passed
    else:
        console.print(
            "\n[dim]Skipping author and proofreader evals (--skip-expensive)[/dim]"
        )

    # Summary table
    console.print("\n[bold]━━━ Overall Results ━━━[/bold]")
    table = Table(show_header=True)
    table.add_column("Agent", style="cyan")
    table.add_column("Result", style="white")

    for agent_name, agent_result in results.items():
        if isinstance(agent_result, bool):
            status = "[green]PASSED[/green]" if agent_result else "[red]FAILED[/red]"
        elif isinstance(agent_result, float):
            status = (
                f"[green]{agent_result:.1%}[/green]"
                if agent_result >= 0.85
                else f"[red]{agent_result:.1%}[/red]"
            )
        else:
            status = str(agent_result)
        table.add_row(agent_name, status)

    console.print(table)


def cmd_show_excluded(content_type: Optional[str] = None, limit: int = 50):
    """Show excluded non-news articles."""
    import json
    from datetime import datetime

    try:
        cache_file = Path("data/processed_articles.json")

        if not cache_file.exists():
            console.print("[yellow]No processed articles cache found.[/yellow]")
            console.print("Run the pipeline first to populate the cache.")
            return

        # Load cache and filter to non-news entries only
        with open(cache_file, "r") as f:
            all_articles = json.load(f)

        cache = {
            k: v
            for k, v in all_articles.items()
            if isinstance(v, dict)
            and v.get("content_type")
            and v.get("content_type") != "news"
        }

        if not cache:
            console.print("[yellow]No excluded articles in cache.[/yellow]")
            return

        # Convert to list and sort by timestamp (most recent first)
        articles = []
        for url_hash, data in cache.items():
            articles.append(
                {
                    "title": data["title"],
                    "content_type": data["content_type"],
                    "timestamp": data["timestamp"],
                }
            )

        articles.sort(key=lambda x: x["timestamp"], reverse=True)

        # Filter by content type if specified
        if content_type:
            articles = [a for a in articles if a["content_type"] == content_type]

        # Limit results
        articles = articles[:limit]

        if not articles:
            console.print(
                f"[yellow]No {content_type or 'excluded'} articles found.[/yellow]"
            )
            return

        # Create table
        table = Table(
            title=f"Excluded Articles (Non-News){f' - {content_type.upper()}' if content_type else ''}"
        )
        table.add_column("Type", style="cyan", width=10)
        table.add_column("Title", style="white", width=80)
        table.add_column("Date", style="green", width=12)

        for article in articles:
            # Format timestamp
            try:
                dt = datetime.fromisoformat(article["timestamp"])
                date_str = dt.strftime("%b %d, %Y")
            except (ValueError, KeyError):
                date_str = "Unknown"

            # Truncate long titles
            title = article["title"]
            if len(title) > 77:
                title = title[:74] + "..."

            table.add_row(article["content_type"], title, date_str)

        console.print(table)
        console.print(f"\n[bold]Total:[/bold] {len(articles)} excluded articles shown")

        # Show summary by type
        type_counts: dict[str, int] = {}
        for article in articles:
            t = article["content_type"]
            type_counts[t] = type_counts.get(t, 0) + 1

        if not content_type and len(type_counts) > 1:
            console.print("\n[bold]By Type:[/bold]")
            for t, count in sorted(type_counts.items()):
                console.print(f"  {t}: {count}")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    # If no command specified, show help
    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Setup logging
    setup_logging()

    # Route to appropriate command
    if args.command == "run":
        asyncio.run(
            cmd_run(skip_publishing=args.skip_publishing, env_file=args.env_file)
        )
    elif args.command == "prework":
        asyncio.run(cmd_prework(env_file=args.env_file))
    elif args.command == "newsletter":
        asyncio.run(
            cmd_newsletter(
                env_file=args.env_file,
                with_prework=args.with_prework,
                skip_publishing=args.skip_publishing,
                force=args.force,
                window_hours=args.window_hours,
                dump_candidates=args.dump_candidates,
                include_sent=args.include_sent,
                plan=args.plan,
            )
        )
    elif args.command == "check-sources":
        sys.exit(
            cmd_check_sources(
                env_file=args.env_file, sample=args.sample, stale_days=args.stale_days
            )
        )
    elif args.command == "onboard":
        sys.exit(cmd_onboard(args))
    elif args.command == "import-pool":
        cmd_import_pool(
            env_file=args.env_file,
            days=args.days,
            include_rejected=args.include_rejected,
            overwrite=args.overwrite,
        )
    elif args.command == "reset":
        cmd_reset(env_file=args.env_file, newsletter=args.newsletter, yes=args.yes)
    elif args.command == "preview":
        asyncio.run(cmd_preview(env_file=args.env_file))
    elif args.command == "test":
        asyncio.run(cmd_test(env_file=args.env_file))
    elif args.command == "config":
        cmd_config(env_file=args.env_file)
    elif args.command == "eval-classification":
        asyncio.run(cmd_eval_classification(dataset=args.dataset, model=args.model))
    elif args.command == "eval-author":
        asyncio.run(cmd_eval_agent("author", dataset=args.dataset, model=args.model))
    elif args.command == "eval-proofreader":
        asyncio.run(
            cmd_eval_agent("proofreader", dataset=args.dataset, model=args.model)
        )
    elif args.command == "eval-summary":
        asyncio.run(cmd_eval_agent("summary", dataset=args.dataset, model=args.model))
    elif args.command == "eval-industry":
        asyncio.run(
            cmd_eval_industry_classifier(dataset=args.dataset, model=args.model)
        )
    elif args.command == "eval-news-type":
        asyncio.run(cmd_eval_news_classifier(dataset=args.dataset, model=args.model))
    elif args.command == "eval-category":
        asyncio.run(
            cmd_eval_category_classifier(dataset=args.dataset, model=args.model)
        )
    elif args.command == "eval-dedup":
        asyncio.run(cmd_eval_agent("dedup", dataset=args.dataset, model=args.model))
    elif args.command == "eval-all":
        asyncio.run(cmd_eval_all(skip_expensive=args.skip_expensive))
    elif args.command == "show-excluded":
        cmd_show_excluded(content_type=args.type, limit=args.limit)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
