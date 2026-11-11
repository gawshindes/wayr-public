# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
poetry install

# Run the full pipeline (with publishing)
poetry run wayr run

# Run pipeline without publishing
poetry run wayr run --skip-publishing
# or
poetry run wayr preview

# Newsletter mode (PIPELINE_MODE=newsletter)
poetry run wayr prework --env-file .env.newsletter                      # discover → classify → score into the pool
poetry run wayr newsletter --env-file .env.newsletter                   # compile the window → Beehiiv draft
poetry run wayr newsletter --with-prework --env-file .env.newsletter    # both, back to back (demo / first issue)
poetry run wayr newsletter --skip-publishing --env-file .env.newsletter # preview HTML to data/, nothing pushed or recorded
poetry run wayr newsletter --dump-candidates --env-file .env.newsletter # scored candidate CSV — calibrate the thresholds from it
poetry run wayr newsletter --window-hours 48 --env-file .env.newsletter # explicit window (lifts the freshness cap); already-shipped stories stay excluded
poetry run wayr newsletter --plan --env-file .env.newsletter            # preflight: what the usual window vs. since-last-issue would yield; runs nothing
poetry run wayr newsletter --force --env-file .env.newsletter           # re-run past the same-day guard
poetry run wayr newsletter --window-hours 48 --include-sent --skip-publishing --env-file .env.newsletter  # inspect incl. stories already shipped
poetry run wayr reset --newsletter --yes --env-file .env.newsletter     # cold start: delete ledger, pool, run log
poetry run wayr import-pool --days 7 --env-file .env.newsletter          # seed the pool from Supabase (newsletter over an existing news deployment)

# Demo newsletters: newsletters/<name>/ holds a newsletter's five files; secrets live once in .env.demo.base
./demo.sh ai-daily env|check|prework|plan|dump|preview|draft|day|reset   # names: wayr-daily ai-daily tech-links austin-local own-site
poetry run wayr onboard <slug> --from tech-links [--answers answers.json]   # a new newsletters/<slug>/ from a few answers (prompts for the rest)

# Production newsletter: newsletters/wayrtoday/ is the live config; the Modal secret is built from it (dry run by default)
poetry run python scripts/modal_secret.py --env-file .env.usa --newsletter wayrtoday [--apply]   # see docs/MODAL_DEPLOYMENT.md "Cutover"

# Run with a custom env file (default: .env)
poetry run wayr run --env-file .env.india
poetry run wayr preview --env-file .env.staging

# Test connections (WordPress in news mode, Beehiiv in newsletter mode, OpenAI)
poetry run wayr test

# Check that every configured feed actually delivers stories (fetch + extract the newest items as discovery would)
poetry run wayr check-sources --env-file .env.newsletter   # ok / blocked / stale / empty per feed; exit 1 if any blocked/empty

# Show current configuration
poetry run wayr config

# Run agent evaluations
poetry run wayr eval-classification      # composed end-to-end (industry → news → category)
poetry run wayr eval-industry            # IndustryClassifierAgent in isolation
poetry run wayr eval-news-type           # NewsClassifierAgent in isolation
poetry run wayr eval-category            # CategoryClassifierAgent in isolation
poetry run wayr eval-author
poetry run wayr eval-proofreader
poetry run wayr eval-summary
poetry run wayr eval-dedup
poetry run wayr eval-all                # run all evals
poetry run wayr eval-all --skip-expensive  # skip author + proofreader (cheaper)
# Every eval run records tokens and cost (reasoning tokens included) as `usage` in its history.json;
# prework and newsletter runs print a per-agent spend line. Prices live in wayr/utils/pricing.py.

# Check word counts of recent published articles
poetry run python scripts/word_count_check.py us        # wayr.today
poetry run python scripts/word_count_check.py india      # india.wayrtoday.com
poetry run python scripts/word_count_check.py staging    # staging.wayrtoday.com
poetry run python scripts/word_count_check.py us -n 30   # last 30 articles

# Run all tests
poetry run pytest

# Run a single test file
poetry run pytest tests/test_models.py

# Run with verbose output
poetry run pytest -v

# Lint
poetry run ruff check wayr/

# Format
poetry run black wayr/

# Type check
poetry run mypy wayr/
```

## Architecture

WAYR (What Are You Reading Today) is an AI-powered tech news aggregation platform. It fetches RSS feeds, classifies articles with LLMs, generates ~300-word AI-written summaries, and publishes them to WordPress — transparently labeling all content as AI-generated.

### Pipeline (Sequential Multi-Agent)

The `WAYROrchestrator` ([wayr/orchestrator.py](wayr/orchestrator.py)) manages this sequential flow:

```
RSS Feeds → NewsDiscoveryAgent → IndustryClassifierAgent → NewsClassifierAgent → CategoryClassifierAgent → AuthorAgent → ProofreaderAgent → PublishingAgent → SocialMediaAgent
```

Classification is a three-stage chain with early exit: industry filter drops off-topic articles, news classifier drops non-news content types, category classifier assigns primary/secondary categories (only runs when content_type == news).

Each stage produces a Pydantic model that wraps the previous stage's output:

```
RSSArticle → ClassifiedArticle → GeneratedArticle → EditedArticle → PublishedArticle
```

### Data Models ([wayr/models/articles.py](wayr/models/articles.py))

- **`RSSArticle`** — raw feed data (title, url, source, content, etc.)
- **`ClassifiedArticle`** — adds L1 `content_type` (news/review/guide/deal/opinion) and L2 `category` (AI, Startup, Hardware, Enterprise, etc.) with confidence scores
- **`GeneratedArticle`** — adds AI-generated title, content, and `sources_cited`
- **`EditedArticle`** — optional proofread version with edit summary and possible category correction
- **`PublishedArticle`** — adds `wordpress_post_id`, `wordpress_url`, `published_at`

### Agents ([wayr/agents/](wayr/agents/))

Each agent extends `BaseAgent` ([wayr/agents/base.py](wayr/agents/base.py)). All LLM-calling agents are **provider-agnostic**: the model is configured as `provider:model_name` (e.g., `google:gemini-flash-latest`, `openai:gpt-4o`, `anthropic:claude-3-haiku-20240307`).

| Agent | File | Role |
|-------|------|------|
| `NewsDiscoveryAgent` | [discovery.py](wayr/agents/discovery.py) | Fetch RSS, URL dedup, non-news filtering, content extraction |
| `DeduplicationAgent` | [deduplication.py](wayr/agents/deduplication.py) | LLM-based same-event dedup on title + feed summary across a rolling window |
| `IndustryClassifierAgent` | [industry_classifier.py](wayr/agents/industry_classifier.py) | Stage 1: filters articles to the tech/business industry scope |
| `NewsClassifierAgent` | [news_classifier.py](wayr/agents/news_classifier.py) | Stage 2: assigns content type (news/review/guide/deal/opinion) |
| `CategoryClassifierAgent` | [category_classifier.py](wayr/agents/category_classifier.py) | Stage 3: assigns primary + secondary news category |
| `AuthorAgent` | [author.py](wayr/agents/author.py) | Generate ~300-word articles from source content |
| `ProofreaderAgent` | [proofreader.py](wayr/agents/proofreader.py) | Edit/review generated articles; can correct category |
| `PublishingAgent` | [publishing.py](wayr/agents/publishing.py) | Post to WordPress REST API |
| `SocialMediaAgent` | [social_media.py](wayr/agents/social_media.py) | Post to Twitter/X and LinkedIn |
| `NewsletterAgent` | [newsletter.py](wayr/agents/newsletter.py) | Newsletter mode: one compile call writes subject line, intro, and per-story emoji/headline/commentary/anchor; blurbs pass through verbatim; falls back to a deterministic issue on failure |
| `ImportanceAgent` | [importance.py](wayr/agents/importance.py) | Newsletter mode: scores each news story 0–1 for the readers. Reader criteria resolve inline var → `NEWSLETTER_IMPORTANCE_CRITERIA_FILE` → [packaged default](wayr/templates/importance_criteria.default.md); [example](wayr/templates/importance_criteria.example.md) shows the shape |

### Deduplication Strategy

Three layers:
1. **Feed ledger** — `FeedLedger` ([wayr/services/feed_ledger.py](wayr/services/feed_ledger.py)) records every item every feed has ever shown us in `data/feed_ledger.json`, plus per-feed ETag/Last-Modified so unchanged feeds cost a 304. The pipeline's delta is "not yet **admitted**", not "not seen" — an item the cap trimmed stays eligible next run. Retention purges on last-seen, never on age.
2. **URL-based** — `ArticleTracker` ([wayr/services/article_tracker.py](wayr/services/article_tracker.py)) caches processed URLs in `data/processed_articles.json`
3. **LLM-based** — `DeduplicationAgent` compares new articles (title plus the feed summary) against published ones from the past `DEDUP_WINDOW_DAYS` to catch same-story, different-source duplicates. Headlines alone miss announcements that outlets anchor on different details; the summary is what exposes the shared event.

Non-news articles (reviews, guides, deals, opinions) are filtered via `NonNewsTracker` ([wayr/services/non_news_tracker.py](wayr/services/non_news_tracker.py)) backed by `data/non_news_cache.json`.

### Configuration ([wayr/config.py](wayr/config.py))

All settings come from `.env`. Model assignments use `provider:model_name` format:

```
MODEL_CLASSIFICATION=google:gemini-flash-latest         # default for all three classifier stages
MODEL_CLASSIFICATION_INDUSTRY=google:gemini-flash-latest  # optional per-stage overrides
MODEL_CLASSIFICATION_NEWS=google:gemini-flash-latest
MODEL_CLASSIFICATION_CATEGORY=google:gemini-flash-latest
MODEL_AUTHOR=openai:gpt-3.5-turbo
MODEL_PROOFREADER=anthropic:claude-3-haiku-20240307
MODEL_DEDUP=google:gemini-flash-latest
MODEL_IMPORTANCE=google:gemini-flash-latest              # newsletter mode
MODEL_NEWSLETTER=google:gemini-flash-latest              # newsletter mode: the compile call
```

Feeds come from `RSS_FEEDS` (comma-separated) or `RSS_FEEDS_FILE` (replaces `RSS_FEEDS`): one source per line — a URL, then optional `type=rss|page` (how to fetch; `page` is reserved for a listing-page adapter and is parsed but skipped with one warning) and `origin=press|brand` (whose words: a news site covering events, or the company announcing its own), then a `#` comment. Unknown tokens are errors. `origin=brand` does three things: the news-type classifier gets the title plus a first-party hint for those items only (press input is byte-identical), a brand post wins a dedup cluster in both discovery and the compile window so the issue links the primary source, and `eval-news-type` reports pass rate per origin. `Source` (`wayr/models/sources.py`) is the model; `settings.sources` is the typed list, `settings.rss_feeds` the plain rss URLs. Requires migration `scripts/migrations/2026-09-22_source_origin.sql` on an existing Supabase project. A newsletter's setup is four reviewable files — sources, reader criteria (`NEWSLETTER_IMPORTANCE_CRITERIA_FILE`, what it picks), a voice guide (`NEWSLETTER_VOICE_FILE`, how it sounds; default is the plain house voice in [voice.default.md](wayr/templates/voice.default.md)), and optionally a section schema (`NEWSLETTER_SECTIONS_FILE`; top-level `intro: false` for a newsletter with no opening; per section: `style`, `tier`, `max`, `categories`, `days`, `show_source`, `show_read_time`, `commentary: false` for blurb-only features — structure lives in the schema, never in the voice guide) — plus an env file pointing at them.

Key behavior controls: `MAX_ARTICLES_PUBLISHED_PER_RUN`, `CLASSIFICATION_CONFIDENCE_THRESHOLD`, `ENABLE_PROOFREADING`, `DEDUP_WINDOW_DAYS`.

Pipeline stage toggles (all default `true`; `ENABLE_PROOFREADING` defaults `false`): `ENABLE_DISCOVERY`, `ENABLE_CLASSIFICATION`, `ENABLE_AUTHOR`, `ENABLE_SUMMARY`, `ENABLE_PUBLISHING`, `ENABLE_SOCIAL_POSTING`. Disabling discovery falls back to cached candidates only; disabling classification passes all discovered articles through as news; disabling author stops the pipeline after classification.

`PIPELINE_MODE` (`news` | `newsletter` | `both`, default `news`) selects the deployment shape. `both` is dual mode: the 2-hourly pipeline researches once, scores importance into the pool, and continues into the per-story branch; the daily compile reads the pool, and stories the site already published stay eligible for the newsletter. In `both` the publish-slot backpressure never skips discovery, so the pool is never starved. Requires both `WORDPRESS_*` and `BEEHIIV_*`. `news` is the per-story WordPress pipeline above and requires `WORDPRESS_*`. `newsletter` runs prework plus a daily compile to the configured publisher, requires that publisher's credentials, and never builds the author, proofreader or per-story WordPress publishing agents (their `ENABLE_*` flags are ignored). It does build the social agent: after a *public* publish (`NEWSLETTER_DELIVERY=publish`, WordPress status `publish`) the issue's link is posted once to LinkedIn and X with the editor's opening and the feature headlines as copy ([newsletter_social.py](wayr/services/newsletter_social.py), no model call); drafts are never shared, and the same tracking files as the per-story path stop a re-run from posting twice. The WordPress issue post carries `NEWSLETTER_WORDPRESS_TAGS` (default `newsletter`) so a front end can filter. See [docs/newsletter-mode-plan.md](docs/newsletter-mode-plan.md).

### Newsletter publishers ([wayr/publishers/](wayr/publishers/))

`NewsletterPublisher` is the adapter contract (`from_settings`, `publish`, `test_connection`); `REGISTRY` maps `NEWSLETTER_PUBLISHER` names to classes: `beehiiv` and `wordpress` (a **draft post** in `NEWSLETTER_WORDPRESS_CATEGORY` via the existing `WordPressClient` — the zero-cost path for a site that has WordPress, and the fallback when the Beehiiv plan lacks the Send API). The settings validator requires the credentials of whichever publisher is named. `NEWSLETTER_DELIVERY=draft|publish` (default draft) is what the publisher does with an issue: WordPress publishes on `publish`; Beehiiv creates drafts until sending is verified on a paid plan. The WordPress adapter strips the template's `<h1>` because WordPress renders the post title itself. `BeehiivPublisher` creates a **draft** — the v2 Create Post call takes only `title` + `body_content`, so the subject line travels in `title` and the post cannot be sent through the API. Creation is asynchronous; the adapter polls to confirm and to pick up `web_url`. **Plan gate:** post creation is Beehiiv's Send API, paid Max/Enterprise only — Launch/Scale and **every trial, including the Max trial** (Beehiiv support ticket 187027, 2026-09-18) return 403 `SEND_API_NOT_ENTERPRISE_PLAN` (and `wayr test` cannot detect it — it hits a read endpoint).

### Newsletter mailers ([wayr/mailers/](wayr/mailers/))

A publisher puts the issue on the web; a **mailer** sends the same HTML to subscribers, wrapped by `wayr/templates/email_default.html` with a read-on-the-web link and the mailer's unsubscribe placeholder. `NewsletterMailer` is the contract (`from_settings`, `send(issue, html, deliver=)`, `test_connection`); `REGISTRY` maps `NEWSLETTER_MAILER`: `none` (default) | `resend-direct` | `resend`. **`resend-direct`** (production) owns no list: it reads `email, unsubscribe_token` of `status='confirmed'` rows from the site's `subscribers` table in Supabase ([subscribers.py](wayr/services/subscribers.py), service-role key, never logs addresses) before every send and emails each one through Resend's batch endpoint (100/request, `Idempotency-Key` per batch, `retry-after` honoured) with that subscriber's `NEWSLETTER_UNSUBSCRIBE_URL` (`{token}` filled) in the footer and a `List-Unsubscribe` header; `NEWSLETTER_DELIVERY=draft` sends to `NEWSLETTER_TEST_RECIPIENTS` only, `publish` to the list; a sending-only key suffices. **`resend`** sends one Broadcast to `RESEND_SEGMENT_ID` with Resend's `{{{RESEND_UNSUBSCRIBE_URL}}}` (needs a full-access key). Both refuse HTML without their placeholder. A mail failure is recorded on the run (`mail_id`/`mail_status`/`mail_error`, migration `2026-11-10_newsletter_run_mail.sql`) and never fails the run — the issue is already on the web and must not ship twice. The mailer is configured in the region env file (`.env.usa`), never in `env.overrides`, so local demo runs have none.

### Supabase migrations

`scripts/supabase_schema.sql` is the from-scratch schema. For an existing project apply `scripts/migrations/*.sql` in date order (idempotent) **before** deploying a branch that writes new columns — mirror writes are best-effort and fail silently otherwise.

### Deployment

Uses [Modal](https://modal.com/) for serverless execution. Production (wayrtoday.com, newsletter mode only) deploys into the **`wayr`** Modal workspace as `wayr-platform-prod-newsletter` with secret `wayr-secrets-prod` and volume `wayr-data-prod` (every environment is `wayr-secrets-<env>` / `wayr-data-<env>`); the retired per-story news app `wayr-platform-prod` sits stopped in the previous, personal Modal workspace. The image carries `wayr/` and `newsletters/`; a production newsletter's secret is assembled by `scripts/modal_secret.py` from the region env file plus `newsletters/<name>/env.overrides`, dropping the `*_FILE` state paths (the `/data` volume defaults apply) and `PIPELINE_MODE` (injected at deploy). See [modal_deploy.py](modal_deploy.py) and [deploy.sh](deploy.sh). The deployment shape is chosen at deploy time by `WAYR_PIPELINE_MODE` (`news` schedules `run_wayr_pipeline`; `newsletter` schedules `run_wayr_prework` and `run_wayr_newsletter`), because schedules are fixed on deploy while secrets are only readable at run time; the deploy injects that mode into every function as `PIPELINE_MODE` after the user secret, and injects `/data/` defaults for all state files before it. `mode_schedules()` in config is the pure, tested selection logic. See [docs/MODAL_DEPLOYMENT.md](docs/MODAL_DEPLOYMENT.md#newsletter-mode).

### Classification Evaluation

`wayr/evaluation/` is organized into per-agent subfolders: `author/`, `proofreader/`, `summary/`, `dedup/`, `classification/`, `industry_classifier/`, `news_classifier/`, `category_classifier/`. Each subfolder contains `eval.py`, `history.json`, and `test_cases.json`. Shared modules (`base_eval.py`, `metrics.py`) stay at the package root. Each of the four classification-family subfolders owns its own copy of the test cases (initially identical, intended to diverge as tests are refined per agent).

- `eval-classification` — runs the full industry → news → category chain end-to-end (integration signal).
- `eval-industry`, `eval-news-type`, `eval-category` — run each classifier in isolation so regressions can be attributed to a single prompt.

The composed evaluator is the integration test; the per-agent evaluators are the iteration signal when tuning a single prompt.
