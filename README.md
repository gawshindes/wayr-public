# WAYR — What Are You Reading Today

An AI-run news desk for a niche: it reads the feeds, keeps the stories that matter to
one audience, and ships them as a daily or weekly **newsletter** — or, in its original
mode, as short AI-written articles on a WordPress site. Everything it publishes is labelled
as AI-generated and links to the original source.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

This is the code behind [wayrtoday.com](https://wayrtoday.com), which runs it in newsletter
mode on a schedule with no human in the loop. The project is published as-is: it is
complete and running, and not actively developed.

## What it does

**Research, continuously.** A job fetches every configured source, remembers every item it
has ever seen so it only pays for the delta, skips items already too old for the next
issue, extracts each article (or uses the text the feed carries when a page refuses), and
drops the thin ones.

**Curate, in three steps.** An industry filter keeps the beat; a news filter removes
deals, opinion, reviews and reports; a category classifier routes what is left. An
importance agent then scores each story 0–1 against the newsletter's own criteria — a
plain-language file you write about your readers — and the score, not a fixed count,
decides what gets in.

**Compile, on the newsletter's cadence.** The compile job takes the window since the last
issue, collapses same-event coverage (a company's own post wins the cluster, so the issue
links the primary source), selects into two tiers, assigns stories to a section schema,
writes a factual blurb per story, and makes one editorial call — subject line, opening,
headlines, a line of commentary — in a voice guide you write. It renders, publishes to
WordPress (or drafts to Beehiiv), emails the confirmed subscribers through Resend, shares
the link on LinkedIn and X, and records the run so no story ships twice.

**Everything is a reviewable file.** A newsletter is a folder of five plain-text files:
sources, reader criteria, section schema, voice guide, and the env overrides that wire them
up. Secrets live once, elsewhere. See [newsletters/](newsletters/) for six ready-made ones
and [newsletters/README.md](newsletters/README.md) for the contract.

```
sources.txt      URL [type=rss|page] [origin=press|brand]   — what to read
criteria.md      who the readers are, what scores high/low  — what to pick
sections.yaml    sections, item styles, caps, routing       — the shape
voice.md         how it sounds (a manner, never lines)      — the voice
env.overrides    paths, cadence, publisher, delivery        — the wiring
```

## Pipeline

```
feeds ─► discovery ─► industry ─► news-type ─► category ─► importance ─► pool
                                                                          │
   daily/weekly compile:  window ─► dedup ─► select ─► sections ─► blurbs ─► compile (voice)
                           ─► render ─► publish (WordPress | Beehiiv) ─► email (Resend) ─► social
```

In **news mode** the same research trunk feeds a per-story branch instead: an author agent
writes a ~300-word article from the source, an optional proofreader edits it, and it is
published to WordPress and shared. `PIPELINE_MODE=both` does both from one research pass.

Every LLM-calling agent is provider-agnostic: a model is `provider:model`, e.g.
`openai:gpt-6-luna`, `anthropic:claude-sonnet-4-6`, `google:gemini-flash-latest`, and each
role (classification stages, dedup, importance, summary, compile, author, proofreader) can
use a different one. Every run ends with a per-agent token and dollar line. Measured in
September 2026 on GPT-6 Luna: a 300-story cold start ≈ $0.35, a steady-state day ≈ $0.10,
a compile ≈ $0.01.

## Quick start: a newsletter, locally, in ten minutes

```bash
poetry install
cp .env.demo.base.example .env.demo.base   # API key(s), a WordPress site for drafts, optional Supabase

./demo.sh tech-links check      # do the feeds deliver stories?
./demo.sh tech-links prework    # discover → classify → score into the pool   (~$0.35 cold)
./demo.sh tech-links dump       # scored candidates as CSV — tune criteria.md from it
./demo.sh tech-links preview    # the issue as HTML under data/tech-links/; nothing pushed
./demo.sh tech-links draft      # compile and push a draft to your WordPress
```

The demos (`wayr-daily`, `ai-daily`, `tech-links`, `austin-local`, `own-site`, `nyc-local`)
each show one shape: a conversational AI daily, a terse link digest, a weekly city
newsletter, a best-of from a single feed. The [demo guide](docs/demo-guide.md) says what
each one demonstrates.

To start your own:

```bash
poetry run wayr onboard fintech-weekly --from tech-links      # a few answers → newsletters/fintech-weekly/
./demo.sh fintech-weekly check && ./demo.sh fintech-weekly prework && ./demo.sh fintech-weekly preview
```

## Commands

```bash
poetry run wayr prework      --env-file .env.x    # research into the pool
poetry run wayr newsletter   --env-file .env.x    # compile the window and publish
poetry run wayr newsletter --skip-publishing      # preview to data/, nothing recorded
poetry run wayr newsletter --plan                 # what the next issue would take; runs nothing
poetry run wayr newsletter --dump-candidates      # the scored pool as CSV
poetry run wayr newsletter --window-hours 48      # explicit window
poetry run wayr check-sources                     # ok / blocked / stale / empty per feed
poetry run wayr test                              # WordPress / publisher / mailer / model keys
poetry run wayr config                            # the resolved settings
poetry run wayr run                               # news mode: the per-story pipeline
poetry run wayr preview                           # news mode, no publishing
poetry run wayr eval-all [--skip-expensive]       # the agent evaluations, with cost
```

## Configuration

All settings come from an env file; [.env.example](.env.example) documents every one. The
ones that decide the shape:

| Setting | What it does |
|---|---|
| `PIPELINE_MODE` | `news`, `newsletter` or `both` |
| `RSS_FEEDS_FILE` | the sources file (or `RSS_FEEDS`, comma-separated) |
| `NEWSLETTER_IMPORTANCE_CRITERIA_FILE`, `NEWSLETTER_VOICE_FILE`, `NEWSLETTER_SECTIONS_FILE` | the three editorial files |
| `MODEL_*` | one `provider:model` per role |
| `NEWSLETTER_PUBLISHER` | `wordpress` or `beehiiv` |
| `NEWSLETTER_DELIVERY` | `draft` (a human sends) or `publish` (no human step) |
| `NEWSLETTER_MAILER` | `none`, `resend-direct` (your own subscriber table) or `resend` (a Resend segment) |
| `NEWSLETTER_MAX_STORY_AGE_HOURS`, `NEWSLETTER_MIN_STORIES`, `NEWSLETTER_MAX_STORIES` | freshness and size |

State is three JSON files: the article pool, the feed ledger, and the run log. Optionally
every stage is mirrored to Supabase ([scripts/supabase_schema.sql](scripts/supabase_schema.sql),
migrations under [scripts/migrations/](scripts/migrations/)).

## Deployment

Scheduled runs use [Modal](https://modal.com/): one deploy gives a 2-hourly research job and
a daily compile, with the newsletter folder baked into the image and a secret built from
your env file by `scripts/modal_secret.py`. [docs/MODAL_DEPLOYMENT.md](docs/MODAL_DEPLOYMENT.md)
has the runbook, including moving a site from per-story posts to a daily digest and wiring
up email. Any cron that can run `poetry run wayr prework` and `poetry run wayr newsletter`
works too.

## Development

```bash
poetry run pytest            # ~475 tests, all offline
poetry run ruff check wayr/
poetry run black wayr/
poetry run mypy wayr/
```

Evaluations for each agent live under `wayr/evaluation/<agent>/` with their test cases and
a history of runs (tokens and cost included); `poetry run wayr eval-news-type` and friends
run one agent in isolation. [CLAUDE.md](CLAUDE.md) is the map of the codebase.

## Docs

- [docs/demo-guide.md](docs/demo-guide.md) — the product today, the demos, what to show
- [docs/newsletter-mode-plan.md](docs/newsletter-mode-plan.md) — the design and its decisions
- [docs/e2e-scenarios.md](docs/e2e-scenarios.md) — every end-to-end run and what it found
- [docs/MODAL_DEPLOYMENT.md](docs/MODAL_DEPLOYMENT.md) — deployment and the production runbook
- [docs/architecture.md](docs/architecture.md) — the per-story pipeline in depth
- [ROADMAP.md](ROADMAP.md) — ideas that were on the list

## Transparency

Every article and every issue says it was generated by AI, cites the sources it was built
from, and links to them. Non-news content (reviews, deals, opinion) is filtered out rather
than rewritten. Emails go only to subscribers who confirmed, and each carries its own
unsubscribe link.

## License

[MIT](LICENSE).
