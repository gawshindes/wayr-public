# Newsletter Mode — Spec & Implementation Plan (v2)

> Supersedes the July draft. v1 assumed newsletter was a second mode bolted onto the
> per-story pipeline. v2 treats the newsletter as the product and the 2-hourly run as
> prework for it.

## Product framing

The product researches all configured sources continuously, identifies genuine news,
and assembles it into a newsletter. Customers are **either** a news site **or** a
newsletter — almost never both — so the two are not co-equal modes competing for
resources. They are two consumers of one shared research pipeline.

That reframing is what shapes this design: the every-2-hour run stops being "the
pipeline" and becomes **prework**. It discovers, dedups, classifies and scores stories
into a pool. The newsletter job wakes on its own cadence, takes the window of stories
gathered since it last ran, and compiles. By the time the newsletter runs, the work is
already done — the engine has been running all day (or all week) for that one send.

## Scope

The MVP is four things and nothing else:

1. **Discovery** — RSS across a configured source list, uncapped, delta-fetched
2. **Curation** — classification and filters, ending in an importance score per story
3. **Newsletter structure** — a configured section schema the draft is assembled into
4. **Publishing a Beehiiv draft** — the human edits and sends inside Beehiiv, so
   "publish" for us means "push a draft"

Supporting decisions inside that scope: story inclusion by **importance threshold**
rather than a fixed count, and window selection by watermark so cadence is a cron
expression and nothing else.

### Explicit non-goals for MVP

Named here so they stay out rather than drifting back in:

- **Email / inbound-pitch ingestion**, and with it embargo handling
- **Non-RSS sources** — Google top-stories or topic search, X, Reddit. Agent tools for
  these are a likely follow-on, but discovery is RSS-only for MVP.
- **Human-in-the-loop story picker** and per-issue editor notes
- **Other publishers** (WordPress, Ghost) via the adapter from
  [multi-publisher-plan.md](multi-publisher-plan.md)
- **Newsletter mode over a news-mode pool** — the compile job scores any unscored window candidate on the spot (`_score_unscored`), persists it, then selects. So a newsletter deployment can sit on top of an existing news deployment's pool with no change to that deployment; newsletter deployments get their own Modal app name so they never replace the news app. Stories the site `published` stay in the window. `wayr import-pool` seeds a local pool from Supabase `pipeline_articles` (`entry_from_pipeline_row` maps rows onto the cache shape, `raw_content` included), so the whole scenario runs locally with no Modal access.
- ~~Dual mode~~ — **also available as `PIPELINE_MODE=both`**, pulled forward for the WAYR-publication test scenario. One intraday pipeline run researches once, scores importance into the pool (the same `_importance_step` prework uses), then continues into the per-story branch; the daily compile reads the pool. Two correctness changes made it real rather than merely wired: the publish-slot gate never skips discovery in `both` (it would starve the pool), and the window query accepts stories the site already `published`. Schedules: pipeline + newsletter, prework unscheduled.
- **Weekly / every-N-day cadence** — the watermark supports it, but ranking quality
  matters far more as the window grows, so daily is the only cadence we validate

### Designed for, not built now

- **Onboarding prompt as ranker.** At setup the customer describes what counts as
  important news for their newsletter, and that prompt becomes the importance agent's
  prompt. This is why importance is a separate agent (below) rather than extra fields on
  the category classifier — per-customer tuning must be one swappable prompt file, not a
  forked classifier.
- **Onboarding command** — ✅ `wayr onboard <slug> --from <demo>` (2026-09-24) writes the
  six-file folder from a few answers (flags, prompts, or a JSON file in the shape a form
  would post): the demo's structure and voice copied, criteria in the customer's words,
  env with paths, category and cadence. Reviewable text only; the first run is
  `./demo.sh <slug> check`. A UI/form is a thin layer over the JSON shape.
- **Onboarding by scraping an existing newsletter** to derive its section structure and
  its source list. This imposes one constraint on MVP work: **the section schema and the
  feed list are exactly the two artifacts that tool will emit**, so both must stay plain
  declarative data (serializable config, no logic) rather than hand-written code.

  Validated against a 29-issue, 13-newsletter corpus — see the appendix. The
  tool's algorithmic core is family classification (section-headed vs story-headed),
  shape matching across issues, sponsor splicing, and redirect resolution. At real
  launch the cheaper path for Beehiiv customers is to **connect their Beehiiv account
  and read their existing posts and template via the API** — the section schema falls
  out of their own published structure with no scraping at all. Scraping remains the
  path for non-Beehiiv prospects.
- **Eval noise.** With the press prompt byte-identical between runs, `eval-news-type`'s
  press pass/fail set still moved by 2–3 of 33 cases per run on both models
  (2026-09-23). The cause is in `BaseAgent._call_openai`: GPT-5 and GPT-6 models
  reject `temperature` while reasoning effort is active, so it is never sent —
  `CLASSIFICATION_TEMPERATURE` has no effect on them and they run at the API default
  with `reasoning_effort=medium`. A single run cannot attribute a ±3 change to a
  prompt edit; run each eval three times and compare the intersection, or try
  `reasoning_effort=none` (Sol/Luna support it; Astra does not) which re-enables
  temperature. Applies to every classifier eval.
- **Model choice, measured 2026-09-23** (GPT-6 Sol/Luna launched that day; runs are
  archived under each eval's `runs/`). Same prompts, same cases:

  | eval | gpt-5.4-nano | gpt-5.6-luna | **gpt-6-luna** |
  |---|---|---|---|
  | news-type (35; brand cases with hint) | 29/35 | 26/35 | 28/35 (brand 2/2) |
  | industry (52) | 47/52 | — | 46/52 |
  | dedup (24, incl. the two-launches case) | 23/24 | — | **24/24**, recall 95.8% |
  | category (50, 9 with content) | 2/11 | — | 2/11 — **fixture defect, not a model result** |

  Reading the 17 failing news-type cases changed the picture. Nine labels were stale or
  misused the taxonomy (`report` for "per reports", a `non-news` label that is not a
  subcategory, `other` where opinion/report/deal fit and all three models agreed) and
  two industry labels were plainly wrong; the industry fixtures' "content" is site
  chrome. Re-scored on corrected labels, from the same archived predictions:

  | | nano | 5.6 Luna | 6 Luna |
  |---|---|---|---|
  | news-type, news-vs-non-news (what ships) | **31/35** | 28/35 | 26/35 |
  | industry | 49/52 | — | 48/52 |

  A full audit of both fixture sets followed (every case, not only the failures —
  passes can be false positives, and one was: Roblox's Kids accounts labeled out of
  scope with both models agreeing). 13 of 35 news-type labels and 4 of 52 industry
  labels were corrected, each with a `relabeled` note in the JSON. Two policy rules
  came out of it and went into the prompt: coverage of a release is news even with
  first impressions (a review evaluates something people can already use), and a new
  event inside an opinion framing — including a named figure's first-time warning or
  claim — is news the first time it is seen (repeats are dedup's job). "In Brief …
  per reports from CNBC" is sourced reporting, not hedging, and not Other. Every
  industry fixture's content opens with site chrome ("Save up to $680 on your Disrupt
  pass… REGISTER NOW", "Benchmarks CLOSED Nifty…"), which the 3,000-char cap turns
  into lost lede — the fixture sets need rebuilding from clean extractions.

  The news-type gap is real, not noise: both Lunas read reported facts with a
  columnist's framing as `opinion` (Mozilla's 271 Firefox bugs, LinkedIn's CEO
  stepping down) and "In Brief … per reports" items as `other`, where nano says news.
  The prompt's Opinion-vs-News section is tuned to nano. One failure is
  model-independent: launch coverage that describes capabilities → `review` (Wired on
  ChatGPT Images 2.0) — the press-side twin of the brand-page problem.

  Re-run with the three prompt edits, on corrected labels (news-vs-non-news, the
  number the pipeline acts on): **GPT-6 Luna 33/35, nano 32/35**; brand 2/2 on
  both; the Wired launch fixed on both; the two shared misses are genuinely
  ambiguous pieces. The nano-tuning was real and the edits removed it.

  Price: GPT-6 Luna $0.10 / $0.50 per 1M in/out versus $0.20 / $1.25 (nano) and
  $0.20 / $1.20 (5.6 Luna); tier-1 TPM 500k versus nano's 200k. Per-token price is
  not the bill, so the evals now record tokens and cost per run (`usage` in each
  `history.json`; reasoning tokens counted). Measured on news-type, 35 identical
  calls: Luna spends ~2× nano's reasoning tokens (8.8k vs 4.6k) and 23% more output
  overall, and still costs half — $0.49 vs $0.98 per 1k calls. `wayr prework` and
  `wayr newsletter` print the same per-agent spend at the end of every run. Recommendation:
  every classifier role, dedup and importance on `openai:gpt-6-luna`. Compile A/B
  (B1's day-2 issue regenerated with GPT-6 Luna writing subject, intro, headlines and
  commentary, 2026-09-23): same shape and voice as the 5.6 Luna draft, a sharper
  opening hook, one formatting quirk — it breaks the line after "**Our take:**" —
  handled by a line in the voice guide. Nothing argues for keeping 5.6 Luna at twice
  the price, so the compile moves too. The category eval must be rebuilt first: 41 of its 50 cases carry no
  content, so every model returns General/General — rebuild all three classification
  fixture sets from pool entries with real extracted content, labels reviewed by hand.
- **Importance and compile evals, per provider.** E2E showed the two newsletter agents defaulting to Gemini while the rest of the stack runs OpenAI. Before choosing, build `wayr/evaluation/importance/` in the existing per-agent convention (eval.py, test_cases.json, history.json) — the labeled set is the customer's own published stories (the onboarding scrape), scored by the model and compared to what the editor actually ran — and run it with `MODEL_IMPORTANCE` set to each candidate (`gpt-5.4-nano`, `gemini-flash-latest`, a Claude tier). Same shape for the compile call: hold-out issues, judge headlines/subject lines against the editor's. Not MVP.
- **Voice guide** — ✅ built after B1's first draft was compared with the Neuron's
  issue of the same day. `NEWSLETTER_VOICE_FILE` is the seam for how the newsletter
  *sounds* (opening, tone, commentary length), the way the criteria file is the seam
  for what it *picks*; the packaged default is the plain house voice. The onboarding
  scrape can draft it from issue text the same way it drafts the section schema.
- **Story picker.** All it needs from MVP is that the importance score and the resulting
  include/exclude decision are **persisted per story** rather than computed and
  discarded — free now, and it lets a future UI show what was left out and why.

## Architecture

Two jobs over one pool.

```
PREWORK (cron: 0 */2 * * *)
  RSS delta fetch → feed ledger → URL dedup → LLM title dedup
    → extract content → industry → news-type → category → importance
    → pool

COMPILE (cron: daily)
  window = [last_run, pipeline_start)
    → select by importance threshold → compile (1 LLM call) → render
    → push draft to Beehiiv → advance watermark
```

Prework never publishes. Compile never discovers or classifies. They share only the
pool, and only prework writes stories to it — which means no concurrent-writer hazard
even if both run on Modal at once.

## Runtime modes

A deployment is one of two things, selected by `PIPELINE_MODE=news|newsletter`
(default `news`, so existing deployments see zero behaviour change).

**`news`** — exactly today. One Modal function, `run_wayr_pipeline`, on the 2-hourly
cron, running `run_pipeline()`: discovery → classification → author → proofread →
summary → WordPress → social. Nothing newsletter-related is constructed or scheduled.

**`newsletter`** — two Modal functions on one shared volume:

| Function | Cron | Entry point | Does |
|---|---|---|---|
| `run_wayr_prework` | `0 */2 * * *` | `run_prework()` | discovery → dedup → extract → classify → importance → pool |
| `run_wayr_newsletter` | `NEWSLETTER_CRON` | `run_newsletter()` | window → select → summarize → compile → render → Beehiiv draft → run log |

`run_wayr_pipeline` is **not scheduled** in this mode.

**Which agents a mode builds is declared, not hardcoded** — ✅ done. `MODE_AGENTS` in
[config.py](../wayr/config.py) maps each mode to the set of agents it may build; an
agent is constructed iff it is in the mode's set **and** its `ENABLE_*` flag (where one
exists) is on. The orchestrator never names a mode — every construction site asks
`self._wants("author", enable_author, "ENABLE_AUTHOR")`, which logs *why* when the
answer is no (excluded by mode, or switched off). At startup `ignored_enable_flags()`
lists any flags that are on but have no agent in this mode, so `ENABLE_AUTHOR=true` in
newsletter mode is visibly a no-op rather than a mystery.

This is deliberately a set, not a sequence. Execution order stays in code and fully
typed; only *membership* is declarative. It is the minimum-viable form of the
"base pipeline and branches" idea: adding a mode or moving an agent is one line in the
table, and the future importance / newsletter / Beehiiv agents are already declared
there so build items 3, 6 and 7 do not touch it. The table lives in code rather than
env for MVP because the agent set has dependencies (importance needs classification);
lifting it to a config file later is trivial.

Today `newsletter` = discovery, dedup, classification, importance, summary, newsletter,
beehiiv; `news` = discovery, dedup, classification, author, proofreader, summary,
publishing, social. Both share the research trunk.

**Shared trunk.** Discovery through classification is identical in both modes, so it is
factored out of `run_pipeline()` into a `_research()` method that both entry points
call. `run_prework()` = `_research()` + importance + pool write. `run_pipeline()` =
`_research()` + author onward, unchanged in behaviour. This is the "base pipeline and
branches" idea at its minimum viable size — no config-driven engine, just one shared
method — and it is what would make dual mode a small change later, though dual mode is
not MVP.

**Blocker found in the current code.** `wordpress_url` and `wordpress_username` are
*required* config fields ([config.py:267-271](../wayr/config.py#L267-L271)) and
`PublishingAgent` is constructed unconditionally
([orchestrator.py:200](../wayr/orchestrator.py#L200)). A newsletter-only `.env` with no
WordPress credentials fails validation before anything runs. Fix: make the WordPress
fields optional, add a `model_validator` that requires the credentials of whichever
publisher the mode actually uses, and construct `PublishingAgent` only in `news` mode.
This is exactly step 8 of [multi-publisher-plan.md](multi-publisher-plan.md), pulled
forward — the two plans converge again.

**Modal specifics.** Schedules are chosen at import time from env, the same way
`WAYR_ENV` already selects secrets and volume: `schedule=modal.Cron(...)` when the mode
wants the function, `schedule=None` otherwise, so an unscheduled function still exists
for manual invocation. Prework and compile may run concurrently on the same volume;
they are safe because they write **disjoint files** — prework owns `feed_ledger.json`
and `processed_articles.json`, compile owns `newsletter_runs.json` and only *reads* the
pool — and every write is already atomic (`tmp` + `os.replace`), so a reader sees old
or new, never half. Keep a `:30` offset on the compile cron anyway as belt and braces.

**Locally.** `.env.newsletter` with `PIPELINE_MODE=newsletter` and Beehiiv credentials;
`poetry run wayr newsletter --with-prework --env-file .env.newsletter` is the whole demo.

## Data model

### 1. Feed ledger — new

Today `fetch_all` pulls every entry from every feed, discovery trims to
`max_articles_processed_per_run`, and **only the survivors are recorded**. Everything
trimmed is forgotten, so we cannot distinguish "new since last run" from "seen and
skipped", cannot measure feed velocity, and cannot detect a dead feed.

The ledger records **every** entry we have ever seen in a feed. Two JSON files on the
data volume (`data/feed_ledger.json`), mirrored to Supabase like everything else:

```
feeds[feed_url] = {
  etag, last_modified,            # conditional-GET state → 304 skips the whole feed
  last_fetched_at, last_status,   # 200 | 304 | error — feed health at a glance
  last_item_count,                # window depth; a sudden drop = feed changed/broke
  consecutive_failures            # dead-feed detection
}

feed_items[url_hash] = {
  url, title, source, feed_url,   # what the feed told us; feed_url = first feed seen in
  rss_published_at,               # source's publish date — the freshness key
  rss_summary,                    # feed's own description, truncated to ~500 chars
  first_seen_at, last_seen_at,    # when it entered / was last present in a fetch
  admitted, admitted_at           # did it go on to the pool? — the link to the pipeline
}
```

**Status: ✅ built** ([wayr/services/feed_ledger.py](../wayr/services/feed_ledger.py)),
with one refinement the code forced. **The pipeline's delta is "not yet admitted", not
"not yet seen."** Today an item the cap trims is deliberately left unrecorded so it is
re-evaluated next run; a ledger whose delta excluded *seen* items would lose every
capped article forever. So the ledger records everything as seen — that is what
coverage and feed health need — but `unadmitted()` is what discovery consumes, and
`admitted` is set only after the cap. Item-level delta therefore saves little work by
itself; the wins are the HTTP-level 304 skip, coverage measurement, and retention that
cannot re-admit a slow feed's window. The Supabase mirror of the ledger is deferred to
build item 9 with the rest of the schema work; local JSON is authoritative as agreed.

**What is deliberately not here:** extracted content. The ledger stores what the feed
*told us* — cheap, complete, ~200 bytes a row. The pool stores what we *did* with the
admitted subset — expensive, selective, with full `raw_content`. Fifteen feeds at ~50
items a day is ~20k rows a month, a few MB of JSON; keeping every row is affordable and
is what makes coverage measurable ("of what the feeds offered, what did we admit?").

`rss_summary` is kept truncated because it is free to capture now and impossible to
backfill later: it is the fallback when extraction fails, the input for a future
classify-before-extract pre-filter, and the descriptor a `link_only` line could carry.
Full-content feeds can ship 10 KB descriptions, hence the cap.

`last_seen_at` is what makes retention safe. An item purged by *age* while still sitting
in a slow feed's window would be re-admitted as "new" on the next fetch. Purge on
`last_seen_at` older than the retention window instead — an item is forgotten only once
the feed itself has forgotten it.

**Trap to avoid:** do not put these in `processed_articles.json`.
[`is_processed`](../wayr/services/article_tracker.py#L258-L260) treats presence at any
stage as "already handled" and `filter_new_articles` drops those, so recording all feed
URLs in the existing tracker would starve the pipeline on the very next run. The ledger
is a separate store; `admitted` is what links it to the pipeline.

### 2. Story pool — extends `pipeline_articles`

The existing table already carries what we need (`created_at` indexed descending,
`raw_content`, the three classifier sub-dicts). Add:

```sql
alter table pipeline_articles add column importance jsonb;
-- {"score": 0.0-1.0, "reason": "...", "scored_at": "..."}
alter table pipeline_articles add column eligible_at timestamptz;  -- indexed
```

**`eligible_at` is the watermark key, not `created_at`** — the moment a story becomes
*publishable*, not the moment we first saw it. For MVP the two are identical: RSS
stories are already published, so `eligible_at = created_at` and prework sets it at
ingest.

It is a separate column anyway because the watermark's correctness rests on eligibility
being monotonic in the window key, and `created_at` is not that key in general. Anything
that becomes eligible out of ingest order sits **behind an advanced watermark forever**
and never ships. The live instance today is a classification failure: such an article
is stranded at stage `discovered` with nothing to retry it
([get_news_candidates](../wayr/services/article_tracker.py#L293) only returns
`category`), so adding retries later without this column would silently lose stories.
Deferred-eligibility sources (embargoed pitches, scheduled content) would land the same
way.

The column costs nothing now and makes those a config change rather than a migration
plus a re-derivation of every historical row.

`created_at` is still set once at entry creation and preserved across stage updates
([article_tracker.py:157-166](../wayr/services/article_tracker.py#L157-L166)); it stays
as the ingestion audit timestamp.

**Deploying to an existing Supabase project:** `supabase_schema.sql` is the from-scratch
schema and cannot add columns to an existing table. Apply
`scripts/migrations/2026-09-16_newsletter_mode.sql` (idempotent) before deploying this
branch in *any* mode — found the hard way in E2E scenario A, where the mirror write
failed silently against prod and a news-mode redeploy would have gone dark the same way.

### 3. Run log — new, replaces a mutable cursor

```sql
create table newsletter_runs (
  id bigserial pk, window_start timestamptz, window_end timestamptz,
  status text, story_count int, publisher text, external_id text,
  draft_url text, created_at timestamptz default now()
);
```

`last_run = max(window_end) where status = 'success'`. An append-only log rather than a
scalar gives the same-day guard, `--force` re-runs, and audit history from one
structure. **The watermark advances only on success** — if compile or push fails, the
window is re-read next run.

**Never send a story twice — a per-story guarantee, not just a window one.** The
watermark stops re-picking only while windows do not overlap, and `--window-hours` and
`--force` make overlap routine (it is how a person runs this by hand). So every run
records the `story_hashes` it contained, and candidates exclude anything in a *successful*
run's list (`NewsletterRunLog.sent_hashes()`); a failed push marks nothing. It lives in the
run log, compile's own file, so compile still never writes the pool. `--include-sent`
lifts the exclusion for inspection. Stories that were in a window but not selected are
not marked, so an overlapping run may still consider them — they never shipped.

**One knob, no hidden clamp.** An earlier draft had `NEWSLETTER_NOMINAL_PERIOD_HOURS` for
the first-run lookback and a `2 × period` outage clamp. Both were redundant with the
freshness cap — a story cannot be ingested before it is published, so anything freshness
keeps is already inside the clamp — and they disagreed (24h on the first run, 36h after).
They are gone. The default window is `[last issue, now)`, never reaching past
`NEWSLETTER_MAX_STORY_AGE_HOURS`; with no issue yet it is simply that bound. Asking for more
is an explicit `--window-hours`.

**After a missed schedule the product asks; the engine does not decide.**
`WAYROrchestrator.plan_newsletter()` (`wayr newsletter --plan`) is a preflight — pure reads,
no model calls, nothing recorded — reporting when the last issue went out, whether a
default run would now drop stories (`missed_schedule`: more time has passed than a story
may age), and what each choice would yield with the `window_hours` to pass:
`usual` (the normal lookback) versus `since_last_issue` (everything missed). That is the
payload for a notification like *"you missed your last send — only the usual stories, or
everything since your last issue?"*. The per-story sent guarantee makes either answer safe.

**Cadence lives with the scheduler and onboarding, not the engine.** Onboarding asks one
question — how often do you send, and when — and derives the cron and
`NEWSLETTER_MAX_STORY_AGE_HOURS` (~1.5× the longest gap between sends: 36 for daily, ~108
for Mon/Wed/Fri, ~250 for weekly). What a customer *usually* does sets the env; what they
do when they deviate is the notification's job.

## Discovery rework

Remove `max_articles_processed_per_run` from the newsletter path entirely. It is
currently doing two jobs badly:

1. **Bounding work.** Replaced properly by delta fetching — we only ever process items
   the ledger has not seen, so steady-state volume is "however much news actually
   broke", which is the correct bound. Note that classification hard-requires scraped
   content ([industry_classifier.py:17-20](../wayr/agents/industry_classifier.py#L17-L20)
   — title is explicitly not a fallback), so each new item costs one HTTP extraction.
   The delta is what makes that affordable.
2. **Choosing what matters.** It does this badly today:
   [`fetch_all` sorts all feeds' items together by `published_date` descending](../wayr/services/rss_fetcher.py#L69)
   and discovery takes the global top N, so one high-velocity feed can consume every
   slot on every run and quiet sources are never sampled. For a product whose value is
   breadth of coverage this is a direct quality defect, and it is invisible today
   because we do not record what we skipped. The ledger surfaces it immediately.

**Two independent deltas, both worth having:**

- **HTTP level** — `feedparser.parse(url, etag=..., modified=...)` with the values
  stored per feed. A `304` means skip the feed entirely this run.
- **Item level** — entries whose `url_hash` is absent from the ledger. This is what
  `filter_new_articles` already does, just applied to the full fetch instead of a
  truncated one.

**Bootstrap behaviour:** the first run against a fresh ledger sees every item in every
feed (~50 × feed count). This is deliberate — it is what makes one-shot mode work — but
it must run concurrently and be bounded by `DISCOVERY_BOOTSTRAP_MAX` so a
fifteen-feed deployment does not open several hundred simultaneous extractions.

## One-shot mode (demo requirement)

The 2-hourly cadence assumes the engine has been running all day. A demo cannot wait for
that, and neither can a new customer's first issue — nobody should have to run for a day
before seeing output. So prework and compile must also be runnable back to back:

```bash
wayr newsletter --with-prework      # one command: discover → curate → compile → draft
```

This works because of the ledger rather than in spite of it: **against a cold ledger the
first prework run is a de facto backfill**, since nothing has been seen yet and the delta
is the entire feed window — roughly a day of stories for most tech feeds. The same code
path serves the demo, customer onboarding, and disaster recovery.

Two things break under it, both of which the 2-hourly cadence was quietly hiding.

### 1. Concurrency — ✅ done

The codebase had no concurrency primitives at all; content extraction and the
classification chain were sequential `for` loops, which uncapped and cold-started would
have been ~15 minutes of dead air. **Shipped:** `map_concurrent` in
[wayr/utils/concurrency.py](../wayr/utils/concurrency.py) (bounded semaphore, results in
input order, per-item exception capture), wired into
[discovery.py](../wayr/agents/discovery.py) and [orchestrator.py](../wayr/orchestrator.py),
controlled by `PIPELINE_CONCURRENCY` (default 10). FIFO ordering and per-article error
attribution preserved. 8 tests. One knock-on: classifier log lines now interleave across
articles; each carries its title snippet so decisions stay attributable.

### 2. Freshness is not the same as window membership

Cold start exposes a conflation the steady state hides. Two different questions:

- *Has this issue already covered the story?* → `eligible_at`, the watermark
- *Is the story actually news?* → `rss_published_at`, the source's publish date

In steady state these agree, because we ingest stories minutes after they publish. On a
cold start they diverge badly: everything in the feed window gets `eligible_at = now`
regardless of whether it published this morning or five days ago, so a one-shot demo
would confidently present week-old stories as today's news. That is exactly the failure
a demo cannot survive.

So selection filters on **both**:

```
eligible_at      in [last_run, pipeline_start)          -- not yet covered
rss_published_at >= pipeline_start - NEWSLETTER_MAX_STORY_AGE_HOURS   -- actually news
```

This is where `newsletter_max_story_age_hours` earns its place back, with a job clearly
distinct from the one v1 gave it: it bounds **story freshness by publish date**, not
window size. Feeds with missing or unparseable dates fall back to `created_at`
([rss_fetcher.py:135](../wayr/services/rss_fetcher.py#L135) already defaults these to
`now`), so they are treated as fresh rather than silently dropped.

### Repeatable demo runs

Demos get run more than once, and the watermark plus the same-day guard will both fight
that. Two flags, with deliberately different semantics:

- `--window-hours N` — override `window_start` explicitly, ignoring the watermark and the
  same-day guard *when choosing the window*. With `--skip-publishing` it is fully
  side-effect-free — the demo flag. A **real** publish under it is recorded like any
  other success (so the watermark, being `max(window_end)` over successes, does move).
  An earlier version of this doc said it "never advances the watermark"; E2E showed three
  back-to-back real pushes under it re-shipping the same nine stories, which is what the
  per-story guarantee below now prevents.
  It also lifts the freshness cap to `N`: asking for 96 hours and being silently capped
  at `NEWSLETTER_MAX_STORY_AGE_HOURS` (36) made every wider window return the same 12
  stories in E2E. In scheduled runs the cap is untouched.
- `--force` — bypass the same-day guard and re-run the *last* window, advancing normally.
  This is the "the draft was bad, regenerate it" flag, not the demo one.

`wayr reset --newsletter` clears ledger, pool and run log for a clean cold start.

## Importance — replaces the fixed story cap

This is the real gap in v1, which sorted `queued_at` **ascending** and took the first N,
i.e. the *oldest* stories in the window. Morning-biased for a daily send, and outright
broken for anything longer — a weekly newsletter would ship Monday's news every Sunday.

**Thresholding, not ranking.** These differ in a way that matters here. Ranking orders
all N stories against each other and takes the top slice; thresholding scores each story
independently against a bar and includes everything above it. Thresholding is simpler,
needs no cross-story comparison, and naturally produces a newsletter that is short on a
slow day and long on a big one — which is what "cap by importance, not a fixed number"
actually means.

It is also the right stepping stone to the onboarding prompt, because "is this
important **to this customer's newsletter**" is inherently a per-story judgment against
a stated criterion, not a global sort.

**New `ImportanceAgent`** ([wayr/agents/importance.py](../wayr/agents/importance.py)),
prompt at `wayr/prompts/importance.md`, model `MODEL_IMPORTANCE`. Runs in prework, only
on stories that survive the category stage. Returns `{score, reason}` persisted to
`pipeline_articles.importance`.

Kept separate from the category classifier deliberately: this prompt is the seam where
the customer's onboarding description lands. Fusing it into the category classifier
would mean forking that classifier per customer.

**Selection at compile time:**

```
candidates = pool where eligible_at in window
                   and rss_published_at >= pipeline_start - MAX_STORY_AGE_HOURS
                   and last_stage = 'category'

featured = [c for c in candidates if c.score >= NEWSLETTER_FEATURE_THRESHOLD]
roundup  = [c for c in candidates if NEWSLETTER_ROUNDUP_THRESHOLD <= c.score
                                   <  NEWSLETTER_FEATURE_THRESHOLD]

if len(featured) < NEWSLETTER_MIN_STORIES:   # slow day — never ship a 2-story issue
    featured = top NEWSLETTER_MIN_STORIES by score
if len(featured) > NEWSLETTER_MAX_STORIES:   # guardrail, not editorial policy
    demote the overflow into roundup rather than dropping it
```

Fixed counts survive only as tail guardrails, the same demotion `max_story_age_hours`
gets. Both the score and the resulting include/exclude decision are persisted, which is
what makes the future story-picker UI possible.

## Compile

The draft we push is a **scaffold, not a finished newsletter**. Customer research shows
editors hand-write the highest-touch sections regardless, and their pain is finding
everything rather than writing it. That lowers the bar on the compile model's prose and
raises it on sourcing completeness — a good trade for the demo.

**LLM budget per issue = N summary calls + 1 compile call.** Explicitly:

- **N summary calls** (`SummaryAgent.generate_summary_from_source`), one per *selected*
  story in a `feature`/`brief` section, producing the blurb from `raw_content`.
- **1 compile call** (`NewsletterAgent`) that receives all selected stories with their
  blurbs and writes the subject line, intro, and per-story emoji, headline and
  commentary. `link_only` items are pure data (source title + URL) and never reach
  either model; `quick_hit` items get a headline from the compile call but no blurb.
- Blurbs pass through **verbatim** — the compile LLM never rewrites them, so no drift
- Any exception falls back to a deterministic newsletter (`llm_generated=False`,
  logged as ERROR) — the send always ships
- Jinja2 render with autoescape, template path configurable, packaged default

**Summarize late.** v1 summarized every story at queue time. Summarize only the
*selected* stories at compile time instead — `raw_content` is already persisted at
discovery ([supabase_store.py:105](../wayr/services/supabase_store.py#L105)), so there
is nothing to re-fetch, and we stop paying for blurbs on stories that never ship.

**Section schema, not category counts.** v1 grouped by `primary_category` and ordered
sections by story count descending. That reshuffles the newsletter's shape every issue,
which is wrong for a product whose readers expect a stable skeleton. Sections are a
configured list, in a fixed order, with identity that does not change day to day.

The schema below was shaped by reading real issues and then validated against a
29-issue, 13-newsletter corpus (appendix). Real newsletters carry more structural
variety than a single story model can express.

### Item styles: the link anchor varies by section

v1 assumed one item shape (`headline` linked, blurb, "why it matters", `Source:` line).
The corpus shows **four distinct link-anchoring patterns**, often several within the same
newsletter, and nothing that needs a fifth (212 sections examined):

| Style | Link sits on | Other fields |
|---|---|---|
| `feature` | source name, bracketed after the prose | emoji, written headline (unlinked), 2–4 sentence blurb, optional "our take" |
| `brief` | subject noun phrase, inline in the sentence | emoji, one-sentence descriptor |
| `quick_hit` | the written headline itself | emoji only, no blurb |
| `link_only` | the source's own headline, verbatim | nothing — a bare roundup line |

Technically Austin runs `feature` in "7 things to know" and `link_only` in "Technical.ly
headlines of the week". The Neuron runs `quick_hit`, a `feature`, and `brief` in "Around
the Horn". So style is a **per-section** setting, and the renderer needs all four rather
than one hardcoded shape.

Two consequences for the story model: items need an **emoji** field (both newsletters
use one per item, and it is chosen per story, not per section), and **source visibility
is a per-section choice** — Technically names the publication as the link text, The
Neuron frequently does not show it at all. v1's unconditional `Source: <a>` footer line
is wrong for three of the four styles.

Headlines are **written, not copied** — "Nurse rachet:", "Entangled by the airport" are
editorial voice, with the link carried separately. That validates the compile agent
writing headlines, and it is why `link_only` is the one style that reuses the source's
own title.

### Most of a newsletter is not stories

Structure is stable across issues of the same publication — confirmed for all eight
dailies with three issues each (appendix). The Neuron's spine, identical in all three of
its issues, is representative:

```
lead story → "what happened in AI today" → main feature → ad
  → AI Skill of the Day → ad → Around the Horn → Treats to Try
  → [weekday feature] → New from The Neuron → A Cat's Commentary → footer poll
```

That validates fixed-schema-as-config. It also overturns an assumption: **of ~13
sections, only 4–5 are story-driven.** The majority is recurring furniture the editor
owns. `placeholder` is the common case, not the exception, and the draft's real job is
filling a handful of story slots inside a mostly-static skeleton — which is the scaffold
framing made concrete.

Item counts are near-fixed too: Around the Horn ran exactly 6 items in all three issues,
Treats to Try 5–7, quick hits 5. Per-section `max` is closer to a target than a
guardrail.

```yaml
sections:
  - {name: "Top Story",           type: stories,     style: feature,   max: 1, tier: feature}
  - {name: "What happened today", type: stories,     style: quick_hit, max: 5, tier: feature}
  - {name: "AI Skill of the Day", type: placeholder}
  - {name: "Around the Horn",     type: stories,     style: brief,     max: 6, tier: feature}
  - {name: "AI",                  type: stories,     style: brief,     max: 4, tier: feature, categories: [AI]}
  - {name: "Monday Meme",         type: placeholder, days: [mon]}
  - {name: "Tuesday Ticker",      type: placeholder, days: [tue]}
  - {name: "Headlines roundup",   type: stories,     style: link_only, max: 8, tier: roundup}
```

`placeholder` emits the heading and an empty block for the editor to fill in Beehiiv.
An empty `stories` section is omitted from the render; a `placeholder` never is.
`categories` is optional; absent means any category.

**Packaged default vs demo schema.** The shipped default is deliberately generic — Top
Story, What happened today, a roundup — and is what a customer gets before onboarding.
For the demo we will know the prospect in advance, so their schema is **hand-built from
their own issues** using `describe_newsletter.py` output and dropped in via
`NEWSLETTER_SECTIONS_FILE`. That is onboarding done by hand once, and it doubles as the
first real test of whether the schema format can express a real newsletter.

**`type` is a support level, not a fixed property of the section.** A section is
`placeholder` because *we cannot currently fill it*, not because it is inherently
manual — memes, polls, sponsor slots and embedded tweets are simply outside what the
pipeline sources today. Treating it that way has two consequences worth building for:
onboarding can **classify** each discovered section as supported or unsupported rather
than requiring a human to hand-label every one, and a section flips to `stories` when a
capability lands (non-news routing making a tools section fillable, for instance) with no
schema rewrite and no change to the renderer. Unsupported sections always render their
heading, so the editor sees the complete skeleton and knows exactly what is theirs.

**Weekday-varying slots are required, not optional.** The same position held "Monday
Meme", "Tuesday Ticker" and "Thursday Trivia" across the three issues — one recurring
slot whose identity depends on the day. Hence the `days` filter. A schema without it
cannot represent a real newsletter, and this was invisible from a single issue.

### Two tiers, so sub-threshold curation is not wasted

Technically's "headlines of the week" is a bare link list — no blurb, no LLM cost, just
title and URL. That is the natural home for stories that cleared classification but
missed the importance bar, which the single-threshold design would otherwise discard
along with everything else behind the watermark:

```
score >= feature_threshold   → full treatment, feeds `tier: feature` sections
score >= roundup_threshold   → bare link, feeds `tier: roundup` sections
below                        → dropped
```

It costs nothing extra — the scores already exist — and it turns curation we have
already paid for into a section readers demonstrably want.

### Assignment

Global `NEWSLETTER_MIN/MAX_STORIES` decides how many stories make the issue; per-section
`max` decides how they distribute. Assignment runs in schema order, highest score first,
each story landing in the first section whose tier and category filter it satisfies and
which still has room, so overflow reaches the catch-all rather than being dropped. A
story appears once, never in two sections.

**Subject line.** The compile call emits a subject line as a distinct field from the
newsletter title; Beehiiv takes it directly and it is the single highest-leverage line
in the whole product. Not a rename of `newsletter_title`.

**Candidate dump — a tuning tool, not a feature.**
`wayr newsletter --dump-candidates` writes the window's full scored candidate list
(title, source, score, reason, included yes/no) to CSV. This is how
both thresholds get calibrated — neither is settable without seeing the score
distribution over real feed data, so this is a prerequisite for the demo, not a
nice-to-have. It happens to be the natural seam for a future story picker.

## Post-MVP — recorded now, built later

Design notes that came out of the corpus and are deliberately **not** in MVP scope. One
has a cheap MVP-scope action attached, called out below.

### Non-news has a home here

"Treats to Try" is tools and products, not news — 5–7 items every issue. Our pipeline
classifies exactly that as non-news and **discards** it via `NonNewsTracker`. Correct for
a news site, wrong for a newsletter, where the reader-facing tools section is a fixture.
Not MVP, but it argues for routing non-news content types into designated sections rather
than dropping them, and for keeping that classification data rather than only using it as
a filter.

### A story can cite several sources — and dedup currently throws them away

Carbon Brief's daily briefing was the outlier in a 14-newsletter corpus: 106 external
links in one issue, with items running 2–3 publication links *each* — one blurb, several
outlets covering the same event, every outlet named and linked. That is standard
press-digest form, and our model cannot express it: the pool is keyed one URL per story,
and the `DeduplicationAgent` **collapses** same-story-different-source down to a single
survivor, discarding the alternates.

For a news site that is right — one post, one canonical source. For a newsletter it
destroys information the editor would have used. The fix is a change of dedup semantics,
not a new agent: **merge into a cluster instead of dropping**, keeping a primary URL plus
alternates on the story. Nothing else in the pipeline changes; the renderer gains an
optional "also: [Outlet], [Outlet]" tail on `feature` items. **MVP-scope action:** the dedup
agent should stop discarding the losers now — record the collapsed alternates on the
survivor. Recording them is free; re-deriving them later is not possible.

### Primary sources are not all RSS — typed sources come after MVP

Newsletters cite primary sources: TLDR's home page (2026-09-18, 50 story links) was 18%
company newsrooms/blogs, 18% tech press, 2% X, and 62% independent blogs, Substacks,
GitHub and arXiv. Ingesting those is a source-type question, not a curation one, and
the split is clean:

- **RSS exists** for most big newsrooms — Apple, OpenAI, Google, DeepMind, Meta,
  Microsoft, NVIDIA, AWS, Hugging Face, GitHub changelog, Samsung, Vercel, Cloudflare
  (validated with `scripts/find_feeds.py`). **MVP treats them as feeds**: lines in
  `sources.txt`, zero code. The consequence is that a launch now arrives twice
  (newsroom + coverage), which is exactly the cluster case above — and the newsletter
  should *prefer the primary link* when it renders the cluster.
- **No RSS**: Anthropic, Meta AI, Mistral, xAI, Tesla, Perplexity, Cursor, Stability;
  locally Statesman, Built In, CultureMap (ABJ blocks bots outright). **Post-MVP:**
  `sources.txt` lines get a type — `rss <url>` (default), `page <url>` (listing-page
  adapter: extract item links + dates, hand off to the existing content extractor),
  later `x @handle`. Adapters emit `RSSArticle`, so the ledger, tracker and every
  classifier stay untouched — discovery already consumes that model. Build `page`
  first; it also unlocks the local outlets B3 cannot reach.
- **X**: no free proxy works (nitter, RSSHub) and the API is paid. Deferred; it is 2%
  of TLDR's links.

**Shape of the typed line** — ✅ built 2026-09-23 (`wayr/models/sources.py`; the
tokens are `type=` and `origin=`, and the first-party value is `brand`, because
"newsroom" names both a news site and a company's press page): a plain URL keeps
meaning `rss` + `press`; optional `key=value` tokens follow it —

```
https://techcrunch.com/feed/
https://openai.com/news/rss.xml            origin=brand
https://www.anthropic.com/news             type=page origin=brand
```

`type` (rss | page) is *how to fetch*; `origin` (press | brand) is *whose words*, and
it exists because B1 showed newsroom content is a different input: the news-type
classifier tagged "GPT-6 Astra: The next generation in intelligence for work" as
`review` and a GSA agreement as `deal` (the taxonomy's Deal means discounts). Two
causes, neither the prompt alone: the classifiers see content only, never the title,
and on a newsroom page the title *is* the announcement while the body is marketing
copy; and openai.com is bot-protected, so the extractor got an 814-char testimonial
fragment. Three consumers of `origin`:

1. The news classifier receives, for brand items only, the title and a first-party
   hint (`SOURCE_CONTEXT`). Press input is byte-identical to before, so the press
   baseline holds; the two brand cases in `news_classifier/test_cases.json` measure
   the brand path (2/2 on gpt-5.6-luna after the change; 0/2 before on both models).
2. The dedup keeper prefers a brand member, in discovery and in the compile window —
   except that a story already in the pool still wins, because it may have shipped.
3. `eval-news-type` reports pass rate per origin, so "brand page vs news site" is a
   measured comparison. `type=page` is parsed and skipped with one warning; a file
   with only page sources is an error.

Also needed for newsroom feeds: when the page refuses (403), fall back to the feed's
own content rather than dropping the item — ✅ built 2026-09-23: a feed body of ≥200
words is used as the content outright (Medium carries whole posts), and any feed text
of ≥`MIN_CONTENT_WORDS` stands in when extraction fails or is thin.


## Beehiiv draft publisher

Use the adapter contract from [multi-publisher-plan.md](multi-publisher-plan.md) — the
two plans converge here.

- `POST https://api.beehiiv.com/v2/publications/{publication_id}/posts`
- `Authorization: Bearer {BEEHIIV_API_KEY}`
- Body: `title`, `body_content` (rendered HTML), **`status: "draft"`**, `subtitle` from
  the intro, `content_tags` from the categories present, and the compile call's
  **subject line** into Beehiiv's email-subject field (confirm exact field name
  against the v2 API docs at build time — do not assume it is `title`)
- Response `id` + `web_url` → `newsletter_runs`
- `test_connection()`: `GET /v2/publications/{id}`

Success means "a draft is waiting in Beehiiv", not "an email went out". No send, no
scheduling — the editor reviews and sends from Beehiiv.

## Config

```python
# Mode — selects which Modal functions are scheduled and which agents are built
pipeline_mode: Literal["news", "newsletter"] = "news"

# WordPress — now optional; a validator requires the active publisher's creds
wordpress_url: HttpUrl | None = None
wordpress_username: str | None = None
wordpress_app_password: str | None = None

# Cadence — deploy-time cron; onboarding derives it and the story-age cap from one question
newsletter_cron: str = "30 6 * * *"

# Structure — declarative so onboarding can generate it later
newsletter_sections_file: str | None = None   # None → packaged default schema

# Selection — two tiers: full treatment, bare roundup link, or dropped
newsletter_feature_threshold: float = 0.6
newsletter_roundup_threshold: float = 0.35
newsletter_min_stories: int = 5               # featured floor on a slow day
newsletter_max_stories: int = 15              # featured ceiling; overflow demotes
                                              # (~13 news items/issue observed across
                                              #  3 Neuron issues — 25 was a bad guess)
newsletter_max_story_age_hours: int = 36      # freshness by PUBLISH date, not window size

# Throughput
pipeline_concurrency: int = 10                # ✅ exists — extraction + classification fan-out
discovery_bootstrap_max: int = 300            # ceiling on a cold-ledger first run

# Agents
model_importance: str = "google:gemini-flash-latest"
model_newsletter: str = "google:gemini-flash-latest"

# Delivery
newsletter_publisher: str = "beehiiv"
beehiiv_api_key: str | None = None
beehiiv_publication_id: str | None = None

# Template
newsletter_template_file: str | None = None   # None → packaged default
```

`ENABLE_AUTHOR` / `ENABLE_PUBLISHING` and the v1 toggle matrix are not needed on this
path — a newsletter deployment simply does not run the author or per-story publish
stages.

**On the compile hour.** Newsroom output is heavily front-loaded — practitioners report
the day's meaningful news lands first thing, by ~11am PT, with stragglers to ~2pm PT.
The window boundary should sit after that peak rather than at an arbitrary hour, or an
issue systematically ships the previous day's news. The default above is a placeholder;
**the real value is the demo prospect's send time minus an editing buffer**, and it is
set at the same time as their section schema, once the prospect is chosen.

## CLI surface

```bash
wayr prework                          # discover → curate → pool (the 2-hourly job)
wayr newsletter                       # compile the window → Beehiiv draft
wayr newsletter --with-prework        # both, back to back (demo / first issue / recovery)
wayr newsletter --skip-publishing     # preview HTML only, no draft, no watermark advance
wayr newsletter --dump-candidates     # scored candidate CSV for threshold calibration
wayr newsletter --window-hours N      # explicit window, watermark untouched (repeatable)
wayr newsletter --force               # re-run last window past the same-day guard
wayr reset --newsletter               # clear ledger, pool and run log
```

## Files

**New:** `wayr/services/feed_ledger.py`, `wayr/agents/importance.py`,
`wayr/prompts/importance.md`, `wayr/agents/newsletter.py`,
`wayr/prompts/newsletter.md`, `wayr/models/newsletter.py`,
`wayr/services/newsletter_renderer.py`, `wayr/templates/newsletter_default.html`,
`wayr/templates/newsletter_sections_default.yaml`,
`wayr/publishers/{__init__,base,beehiiv}.py`, `wayr/services/newsletter_runs.py`,
plus tests for ledger / importance / agent / renderer / publisher / window selection
/ section assignment.

**Already landed:** `wayr/utils/concurrency.py`, `PIPELINE_CONCURRENCY` in config,
concurrent extraction in `discovery.py` and classification in `orchestrator.py`,
`tests/test_concurrency.py`.

**Modified:** `wayr/config.py` (`PIPELINE_MODE`; WordPress fields optional +
per-publisher credential validator; newsletter settings),
`wayr/services/rss_fetcher.py` (etag + conditional GET),
`wayr/agents/discovery.py` (ledger write, cap removal),
`wayr/agents/deduplication.py` (record collapsed alternates),
`wayr/orchestrator.py` (`_research()` factored out; `run_prework()` /
`run_newsletter()`; mode-conditional agent construction),
`wayr/agents/summary.py` (`generate_summary_from_source`),
`wayr/cli.py` (`wayr prework`, `wayr newsletter` + flags, `wayr reset`),
`modal_deploy.py` (second cron, jinja2),
`scripts/supabase_schema.sql`, `pyproject.toml`, `.env.example`, `CLAUDE.md`.

## Build order

Each slice is PR-sized; the order follows the dependencies.

0. ✅ **Mode selector and boot fix** — `PIPELINE_MODE`, WordPress fields optional with the
   per-publisher validator, `PublishingAgent` constructed only in `news` mode. Nothing
   newsletter-shaped can even start without this.
1. ✅ Feed ledger, delta fetch (HTTP + item level), cap removal, `last_seen_at` retention
2. ✅ `_research()` factored out of `run_pipeline()`; `run_prework()` on top of it. A minimal `wayr prework [--env-file]` command came with it so prework is runnable end to end before item 8 fills out the rest of the CLI surface.
3. ✅ Pool columns (`importance`, `eligible_at`), `ImportanceAgent` and its prompt. The prompt asks for an anchored 0–10 integer (normalized to 0–1) whose anchors line up with the two thresholds: 6+ ≈ feature (0.6), 4–5 ≈ roundup (0.35). Reader criteria resolve inline var → `NEWSLETTER_IMPORTANCE_CRITERIA_FILE` → packaged default (`wayr/templates/importance_criteria.default.md`, `{{INDUSTRY}}`-substituted). The file is the per-customer seam and what onboarding will emit; `wayr/templates/importance_criteria.example.md` is a filled-in example for a fictional newsletter showing the shape — readers, priorities, house rules, and **calibration examples** pinning each score anchor to concrete stories, the section onboarding can derive from a customer's own issues. An unparseable reply records a neutral 0.5 with `parsed=False` so system failures show in the candidate dump rather than silently dropping a story. `importance` never advances `last_stage`.
4. ✅ Run log, watermark, two-tier selection with the freshness filter. `NewsletterRunLog` is append-only JSON; the watermark is `max(window_end)` over successes and advances only when a publish succeeds (item 7 records it). `compute_window()` handles bootstrap, steady state, the 2×period gap clamp, and the `--window-hours` override. `select_stories()` is a pure function with an explicit `unscored` bucket. The tracker now stores `rss_published_at` (UTC-normalized) so freshness is filtered on the source's publish date, independent of the ingestion window. `run_newsletter()` runs to selection; compile/render/publish continue from its result.
5. ✅ Section schema loader, newsletter models, renderer — four styles, `placeholder`, `days`. `SectionSchema` loads YAML and is validated at startup (a bad file fails fast). `assign_stories()` is pure: schema order, tier → category → capacity, once only, empty story-sections dropped, placeholders kept, `unplaced` reported. The renderer works on unwritten stories via deterministic fallbacks (source title, no emoji), which is the always-ships path; item 6 fills the written fields. `feature` with `show_source: false` links the headline instead so the link is never lost. Jinja2 and PyYAML are now declared dependencies and in the Modal image.
6. ✅ `NewsletterAgent` compile; summarize-at-compile via `SummaryAgent`. One JSON-output call writes subject, title, intro and per-story emoji/headline plus commentary (feature) or `anchor_text` (brief); link_only never reaches the model; blurbs pass through verbatim; a skipped story keeps its fallbacks, a bad reply raises and the deterministic issue ships. **Plan correction:** compile-time blurbs cannot rely on Supabase `raw_content` since local JSON is authoritative, so news-stage tracker entries now persist capped extracted content and compile resolves text tracker → ledger RSS summary → re-extract → empty.
7. ✅ Beehiiv draft publisher on the adapter contract, and the watermark now advances. **Checked against the v2 docs:** Create Post takes only `title` + `body_content` — no `status`, `subject_line`, `subtitle` or `content_tags` — so a created post cannot be sent through the API and lands as a draft; the subject line travels in `title` (Beehiiv defaults the email subject to it). Update Post documents only content fields too, so setting subject/subtitle/tags after creation is a post-MVP question. Creation is asynchronous (201 → 202 while processing → 200, or 404 `POST_CREATION_FAILED`); the adapter polls briefly to confirm and pick up `web_url`, and treats creation failure as a failed publish. `<style>` blocks are stripped on ingest — the default template already uses no style block. `run_newsletter()` records a `success` run and advances the watermark only after `publish()` returns; a failed push records a `failed` run (auditable, not blocking the next attempt); `--skip-publishing` records nothing. Orchestrator tests now stub the LLM agents and publisher by default so no test can reach the network.
8. ✅ CLI surface and one-shot mode; `wayr reset --newsletter`. `wayr newsletter` carries `--with-prework` (one-shot), `--skip-publishing` (preview HTML to data/, nothing pushed or recorded), `--dump-candidates [PATH]` (scored CSV with tier per row — the threshold calibration tool; implies preview and excludes persisted content), `--window-hours N` and `--force`. Exit 1 only on a failed push; `already_sent` and `empty` exit 0. `wayr reset --newsletter` deletes ledger, pool and run log, prompts unless `--yes`, and refuses in a non-interactive session without `--yes`.
9. ✅ Modal functions and mode-aware schedules, schema SQL, `.env.example`, `CLAUDE.md`. `run_wayr_prework` and `run_wayr_newsletter` are deployed alongside the pipeline function; which get a cron is decided at deploy time by `WAYR_PIPELINE_MODE` through the pure, tested `mode_schedules()`. Because secrets are only readable inside a running function, the deploy injects that mode as `PIPELINE_MODE` via `Secret.from_dict` *after* the user secret (so it always wins) and `/data/` defaults for every state file *before* it (so the secret can override). Each newsletter function also forces its own mode in-process. `run_wayr_newsletter(skip_publishing, with_prework)` doubles as the Modal preview and one-shot; the local entrypoint dispatches by mode. Docs, deploy.sh and the quick reference cover the newsletter deployment.

Then the E2E list below, starting with the timed one-shot run.

## Verification

**Unit:** ledger dedup and delta computation; `304` short-circuit; importance
thresholding at both tails (backfill on a slow day, ceiling on a big one); section
assignment respects schema order and per-section `max`, with overflow reaching the
catch-all; watermark window boundaries including the mid-run race; watermark does
**not** advance on publish failure; compile fallback on malformed LLM JSON; renderer
escaping and broken-template fallback; Beehiiv publisher posts `status=draft`.

**E2E for the demo:**

1. **`wayr newsletter --with-prework` from a clean reset completes in minutes and
   produces a Beehiiv draft.** This is the demo itself, so it is the first thing that
   has to work, not the last. Target: comfortably under five minutes end to end.
2. Fresh ledger, one run of prework → every feed item recorded, only new ones extracted
   and classified, importance scores present
3. Second prework run 2h later → only the delta is processed, `304` on unchanged feeds
4. Cold-start freshness → a story still in the feed but published four days ago is
   scored but **excluded** from the issue by the publish-date filter, rather than
   appearing as today's news
5. `wayr newsletter --dump-candidates` over a real day → inspect the score distribution
   and set both thresholds from it. **Do this before step 6** — they cannot be picked in
   the abstract, and everything downstream depends on them.
6. `wayr newsletter --skip-publishing` → preview HTML, correct window, sections in
   schema order, each rendering in its configured style with the link on the right
   anchor, `placeholder` sections present and empty, story count varying with the news
   day rather than pinned to a constant
7. Real run → draft appears in Beehiiv with the right subject line, `newsletter_runs`
   row written, watermark advanced
8. Immediate re-run → same-day guard trips; `--force` overrides
9. Simulated push failure → watermark unchanged, next run re-reads the same window

**E2E log — scenario A (2026-09-16), newsletter mode over wayrtoday's pool.** Steps 1–3
pass: `import-pool` brought 54 stories with content from Supabase; `wayr test` 200 on the
real publication; the 48h dump scored 25 candidates and produced a correct preview (feature
link on the source name, briefs on model-written anchor phrases, roundup verbatim titles;
subject line "Google patches zero-click Pixel modem exploit") in 28s. **Calibration:** over
the 7-day pool (51 scored) the distribution is 0.1×3 0.2×8 0.3×5 0.4×16 0.5×4 0.6×9 0.7×4
0.8×2 — the model reaches 0.8 for genuinely large events (Miro acquisition, IDScan breach)
and reserves 0.9–1.0; with the defaults that is ~2 featured and ~3 roundup stories per day,
which reads editorially right. **Keep 0.6 / 0.35.** Three defects found and fixed: Gemini
5xx/429 were not retried (7 of 25 candidates lost to 503s on the first run); no LLM call
had a timeout (a hung Gemini call stalled a whole run indefinitely — now a 90s per-call
ceiling in `BaseAgent`, retried then surfaced); and the from-scratch schema cannot add
columns to an existing table, so prod lacked `importance`/`eligible_at`/`newsletter_runs`
and every mirror write failed silently — `scripts/migrations/2026-09-16_newsletter_mode.sql`
is the prerequisite for deploying this branch in any mode.

**E2E step 4 (2026-09-17):** the push returned 403 `SEND_API_NOT_ENTERPRISE_PLAN`. The
run was recorded `failed` locally and in prod `newsletter_runs`, the watermark did not
advance — the contract held. The finding is product-level: **Beehiiv's Send API and RSS
to Send are Max/Enterprise only**, so "push a draft to Beehiiv" serves Max/Enterprise
customers (the 700k-sub prospect is one) and not Launch/Scale publications, including any
newly created one. The publisher adapter seam is the answer — and it was exercised the same day:
`WordPressNewsletterPublisher` (`NEWSLETTER_PUBLISHER=wordpress`) lands the issue as a
draft post in a configured category via the existing client, one file plus a registry
line, exactly as the seam was designed. The mode table's agent key is now
`newsletter_publisher` and the validator requires the named publisher's credentials.

**The demo's real acceptance test** is not any of the above passing — it is running
prework for a full day against real feeds and having an editor confirm the draft caught
the stories that mattered. Sourcing completeness is the product; the plumbing above just
has to not lose anything.

Plus `pytest`, `ruff`, `mypy` green.

---

## Appendix — Research findings (evidence behind the design)

Produced with [`scripts/describe_newsletter.py`](../scripts/describe_newsletter.py) over
[`scripts/newsletter_corpus.csv`](../scripts/newsletter_corpus.csv): 29 issues, 13
newsletters, 8 of them with three issues each. Kept here so the decisions above stay
traceable to evidence; nothing in this appendix is a build item.

[`scripts/describe_newsletter.py`](../scripts/describe_newsletter.py) is the research
front end for this — deliberately descriptive, emitting raw observations rather than
mapping pages onto the schema above, so that a style or slot we have not thought of
shows up as an anomaly instead of being normalised away.

Running it over three Neuron issues established that **section identity is not the
heading text**. Only five headings recur verbatim; twenty appear once. Yet almost all
of those twenty are the same recurring slots with variable headings — "AI Skill of the
Day: *<topic>*", sponsor blocks with wholly arbitrary titles, and a weekday slot that
reads "Monday Meme" / "Tuesday Ticker" / "Thursday Trivia". Matching sections across
issues therefore needs pattern matching — emoji prefix, stable prefix before a colon,
document position, and block shape — not string equality. **That resolution problem,
not HTML parsing, is the onboarding tool's actual algorithmic core.**

A 13-newsletter corpus then ruled out the obvious shortcut. Heading *level* carries no
consistent section-versus-item meaning: The Neuron puts sections in `h1`, The Daily
Upside in `h2`, The Rundown in `h3`/`h4`, and TLDR uses `h3` for individual *stories*
with its section headers not in heading tags at all. Any "group by h2" rule is wrong
for most of the corpus. Section detection has to come from structure — runs of
same-shaped blocks, link density, position — not from the tag name.

With three issues each of eight dailies, the corpus splits into **two structural
families** that need different detection:

- **Section-headed** (The Rundown, The Neuron, The Skimm): sections carry stable named
  headings and stories are list items beneath them. Rundown recurs 8 headings verbatim
  at fixed positions across all three issues. Heading recurrence finds the skeleton
  directly; the only wrinkle is variable-suffix slots ("AI Skill of the Day: *topic*").
- **Story-headed** (TLDR, Carbon Brief, The Daily Upside, Next Draft): every *story*
  gets its own heading and the section structure is implicit — TLDR's five daily
  sections are not heading tags at all. Heading recurrence scores TLDR as the least
  stable title in the corpus when it is plausibly the most rigid. Its skeleton is
  visible only as a *shape*: runs of same-level single-link headings, whose lengths
  line up issue to issue.

The onboarding tool must classify a newsletter into a family first, then apply the
matching detector. A single heading-based approach is correct for one family and
silently wrong for the other.

Measured by *shape* — the per-issue sequence of single-link runs and multi-link
blocks — **all eight dailies are structurally stable**, which settles the premise the
section schema rests on. Next Draft is perfectly rigid (identical shape, three
issues). Rundown and Daily Upside vary only at the sponsor slot. Carbon Brief and
Skimm vary block *count* with the news day, as a briefing should — stable skeleton,
elastic sections. Two details for the tool: TLDR's sponsor blocks sit *inside*
sections (after the first two or three headlines), so a sponsor is an interruption to
splice across, not a section boundary; and click-tracking redirects (Skimm routes
~90% of links via `skimmth.is`, SmartBrief via `r.smartbrief.com`) hide the real
source domain, so source-list extraction has to resolve redirects before it can
attribute anything.
