# Newsletter E2E — three scenarios

Each scenario is its own `.env` file **and its own data directory**. The pool, feed
ledger and run log are per-deployment state; if two scenarios share `data/`, they share
one watermark and one dedup cache and trample each other — and scenario A would overwrite
the real WAYR cache in `data/`. `.env*` is gitignored, so these files are safe to create.

"Standard Beehiiv template" here means: we push plain `body_content` HTML and the
publication's own design wraps it. That is what the adapter does; there is nothing to
configure on our side. A `NEWSLETTER_SECTIONS_FILE` is optional in all three — the
packaged default schema (Top Story / What happened today / Also worth knowing) is the
"standard" structure.

## Prerequisite for Beehiiv: the publication must be on a **paid** Max or Enterprise plan

Found in scenario A step 4: Beehiiv's post-creation API is part of its **Send API**, which
the pricing page lists only under Max ($96/mo) and Enterprise. Launch and Scale include
"API Access (excluding Send API)" — reads work, `POST /posts` returns 403
`SEND_API_NOT_ENTERPRISE_PLAN`. "RSS to Send" is Max/Enterprise too, so there is no
automated ingestion path into a lower-plan publication. A brand-new publication (scenarios
B and C) starts on Launch and hits the same wall. Trials do not help: Beehiiv support
(ticket 187027, 2026-09-18) confirmed the Send API is excluded from every trial, the Max
trial included — only a paid Max plan unlocks it. `wayr test` passes regardless, because it
exercises a read endpoint. Options: upgrade the publication, or use another publisher
adapter (WordPress draft for a site that has one; the preview HTML pasted into Beehiiv's
editor by hand for Launch/Scale).

## Prerequisite for any Supabase-connected env: apply the migration

`scripts/migrations/2026-09-22_source_origin.sql` adds `source_origin` (typed sources;
without it the mirror drops every discovery batch, logged at ERROR).
`scripts/migrations/2026-09-16_newsletter_mode.sql` adds `importance`, `eligible_at` and the
`newsletter_runs` table. Until it is applied, every Supabase mirror write from this branch
fails silently (best-effort) — including the news pipeline's own audit trail after a
redeploy, because discovery now writes `eligible_at`. Paste it into the Supabase SQL
editor; it is idempotent.

## Common to all three

```bash
OPENAI_API_KEY=...          # required field; classification fallback
GOOGLE_API_KEY=...          # dedup, importance, summary, compile default to Gemini
BEEHIIV_API_KEY=...         # Beehiiv → Settings → Integrations → API
LOG_LEVEL=INFO
```

Run order per scenario (steps 1–3 write nothing to Beehiiv):

```bash
E=.env.scenario-a   # or -b, -c
poetry run wayr test --env-file $E                                          # 1. credentials, read-only
poetry run wayr reset --newsletter --yes --env-file $E                      # 2. cold start
time poetry run wayr newsletter --with-prework --skip-publishing --env-file $E   # 2. timed one-shot → data/<dir>/newsletter_preview_<date>.html
poetry run wayr newsletter --dump-candidates --window-hours 48 --env-file $E     # 3. score distribution → set the two thresholds
poetry run wayr newsletter --with-prework --env-file $E                     # 4. the real thing: creates a DRAFT in Beehiiv
```

Step 4 writes into the Beehiiv account. A draft never sends and can be deleted from the
dashboard, but it is a real write — trigger it deliberately.

---

## A — newsletter mode on the existing wayrtoday.com pool

The news pipeline already runs every 2 hours on Modal and fills the pool on the
`wayr-data-dev` volume. Scenario A compiles a newsletter **from that pool**, with no
change to the news deployment and no dual mode.

Two things make this work without touching prod:

- **The compile job scores what it finds.** A news-mode pipeline never runs the
  importance agent, so every story in that pool is unscored. `run_newsletter()` now
  scores any unscored candidate in its window on the spot (content from the tracker,
  the ledger, or a re-extract), persists the score, and then selects. Stories the site
  already `published` are in the window too — the newsletter is the digest of the day.
- **Seed a local pool from Supabase — no Modal involved.** The news pipeline mirrors
  every story to `pipeline_articles`, *with* `raw_content`. `wayr import-pool` reads the
  last N days for your `REGION` into `ARTICLE_CACHE_FILE`, so blurbs need no
  re-extraction and nothing on Modal is touched.

```bash
cp .env.usa .env.scenario-a      # then edit the keys in the table below
poetry run wayr import-pool --days 7 --env-file .env.scenario-a
```

```bash
# .env.scenario-a
PIPELINE_MODE=newsletter
BEEHIIV_PUBLICATION_ID=pub_...            # the existing WAYR publication
INDUSTRY=Technology
INDUSTRY_CATEGORIES=AI,Startup,Hardware,Enterprise   # WAYR's list, for the criteria default
RSS_FEEDS=...                              # WAYR's feed list — only used if you also run prework

ARTICLE_CACHE_FILE=data/scenario-a/processed_articles.json   # import-pool writes here
WP_TAXONOMY_CACHE_FILE=data/scenario-a/wp_taxonomy.json
FEED_LEDGER_FILE=data/scenario-a/feed_ledger.json
NEWSLETTER_RUNS_FILE=data/scenario-a/newsletter_runs.json
```

Do **not** run `--with-prework` or `wayr reset` here — the pool is the point. Imported
entries carry what the news pipeline recorded: stage (`category` or `published`),
classification, `raw_content` as blurb text, and `rss_published_at`. What it never wrote
— importance scores — the compile job fills lazily on its first pass over the window.
`eligible_at` falls back to `created_at`.

```bash
poetry run wayr test --env-file .env.scenario-a
poetry run wayr newsletter --dump-candidates --window-hours 48 --env-file .env.scenario-a   # scores the pool; CSV
poetry run wayr newsletter --skip-publishing --window-hours 48 --env-file .env.scenario-a   # preview
poetry run wayr newsletter --window-hours 48 --env-file .env.scenario-a                     # real draft into WAYR's publication
```

**If the publication is on Launch/Scale** (WAYR's is), Beehiiv rejects the draft with 403
`SEND_API_NOT_ENTERPRISE_PLAN`. Scenario A can complete on WAYR's own WordPress instead —
the newsletter lands as a **draft post** in the "Newsletter" category on `cms.wayrtoday.com`,
never published, using the credentials already in the env:

```bash
# .env.scenario-a — add:
NEWSLETTER_PUBLISHER=wordpress
NEWSLETTER_WORDPRESS_CATEGORY=Newsletter
```

`wayr test` then checks the WordPress connection. Switch back to `beehiiv` once the
publication is on Max.

Re-running is safe: a story that shipped in a successful issue is never picked again,
even with an overlapping `--window-hours`; failed pushes mark nothing. `--include-sent`
shows them for inspection. A wider `--window-hours` also widens the freshness cap.

`--window-hours 48` because an imported pool has no run log; with `--skip-publishing` it records nothing and
leaves the watermark alone, so you can preview freely. What to look for: the lazy-scoring line in the log
("Scoring N candidates with no importance yet"), a sensible score spread in the CSV
over stories you recognise from the site, and a draft whose links go to the external
sources (not to wayrtoday.com posts — a per-section `link_target` is a plausible
follow-up for this scenario, not built).

If you later want this scheduled on Modal, deploy newsletter mode with
`WAYR_ENV=prod`: it gets its own app (`wayr-platform-prod-newsletter`) sharing prod's
secret and volume, so the news app is untouched. The compile job then writes importance
scores into the shared pool file; the `:30` cron offset keeps it clear of the news
pipeline's `:00` tick.

## B — three new newsletters, one per archetype

> Folders were renamed when they became demos (2026-09-24): `b1-ai-daily` → `ai-daily`,
> `b2-tech-links` → `tech-links`, `b3-austin-local` → `austin-local`, `c-own-site` → `own-site`,
> and scenario A lives on as `wayr-daily`. `./demo.sh <name> <step>` replaces the per-scenario
> env files; see `newsletters/README.md`. The findings below keep their original names.

Three use cases, each a folder under `newsletters/<slug>/` holding everything that makes
a newsletter *this* newsletter: `description.md` (the brief), `sources.txt` (validated
feeds, `RSS_FEEDS_FILE` format — `origin=brand` marks a company's own newsroom,
`type=page` a listing page the pipeline accepts but cannot fetch yet), `criteria.md` (the importance prompt), `sections.yaml`
(the structure) and `env.overrides` (the settings that wire those in and isolate state).

| Case | Archetype | Shape | What it tests |
|---|---|---|---|
| `b1-ai-daily` | The Neuron | verbose, few links: lead + quick hits + deep dive + one-liners | feature-heavy selection, commentary quality, placeholders |
| `b2-tech-links` | TLDR | terse, many links: five fixed sections of one-liners | breadth — 24 feeds, 12–30 slots, a low roundup bar, category routing |
| `b3-austin-local` | Technically Austin | weekly: "7 things to know" + headlines of the week | **expansion** — general local-news feeds, geographic relevance carried by criteria + industry |
| `c-own-site` | a site's own feed | weekly digest: 3–4 featured + links | one first-party source, classification off, criteria alone curate |

### Building an env file

Secrets are never in the folder. Layer the overrides on a copy of a working env; later
keys win:

```bash
cat .env.scenario-a newsletters/ai-daily/env.overrides > .env.scenario-b1   # likewise b2, b3
poetry run wayr config --env-file .env.scenario-b1                              # check what resolved
```

The overrides set `PIPELINE_MODE=newsletter`, the four `*_FILE` state paths under
`data/<slug>/`, `REGION=<slug>` (the prod Supabase mirror stays on, but its rows are separable from `us` and purgeable by region), the
WordPress publisher, and a freshness cap that clears the longest gap between issues
(96 h for a weekday daily so Monday still sees Friday evening; 240 h for a weekly).
The overrides point `WORDPRESS_URL` at staging.wayrtoday.com so a test draft can never
land on the production CMS; add a staging `WORDPRESS_USERNAME` / `WORDPRESS_APP_PASSWORD`
to each assembled `.env.scenario-b*` file (they are gitignored).

### Where the sources came from

`scripts/find_feeds.py` discovers feed URLs for a domain list (declared `<link
rel=alternate>`, then well-known paths), validates each with feedparser (items, newest
age, words carried) and writes a candidates file with the evidence per line and the
rejects as comments. Two ways in:

```bash
# from the corpus: domains the archetype's issues actually linked to
poetry run python scripts/find_feeds.py --from-describe .cache/corpus_run6.json \
    --newsletter neuron --newsletter "Rundown AI" --out newsletters/ai-daily/sources.candidates.txt
# hand-picked: exact feed URLs and seed domains, validated the same way
poetry run python scripts/find_feeds.py --seed kut.org --feed https://www.kxan.com/feed/ --out ...
```

Finding from the corpus taught the important thing: **what a newsletter links to is
not what it sources from.** The Neuron's top linked domains are OpenAI, X, Anthropic and
its own sponsor; TLDR's are a job board and one-off product pages. Citations are the
output of curation — the RSS sources are the outlets that *cover* those announcements.
So B1 and B2 use curated, validated tech-press lists (`sources.candidates.txt` is kept
as the record of the corpus attempt). B3 is the inverse: local outlets are both the
sources and the citations, and the probe mapped the terrain — KUT, Chronicle, KXAN,
KVUE, Fox 7, Community Impact, UT News and three statewide feeds are live; Austin
Monitor and Silicon Hills feeds died months ago; ABJ/Austin Inno, Statesman, Built In
and CultureMap have no public feed. Those last are the outlets Technically cites most,
which is the onboarding follow-up for a real local customer (scraping adapter or paid
feed).

### Running a case

```bash
poetry run wayr check-sources --env-file .env.scenario-b1                              # first: which feeds actually deliver stories (page lines show as skipped)
poetry run wayr newsletter --with-prework --skip-publishing --env-file .env.scenario-b1  # cold start → data/newsletter_preview_*.html
poetry run wayr newsletter --dump-candidates --env-file .env.scenario-b1                # score spread against criteria.md
poetry run wayr newsletter --plan --env-file .env.scenario-b1                           # what the next issue would take
poetry run wayr newsletter --env-file .env.scenario-b1                                  # real draft, watermark advances
```

B2 prep (2026-09-23): `check-sources` → 30 ok, 1 blocked (Netflix Tech Blog: Medium
gives the extractor 0 words), 1 timeout (Samsung, kept), 5 page lines skipped. The
Netflix case fixed something general: Medium feeds carry the whole post in
`content:encoded`, and the fetcher threw it away. Now a feed body of ≥200 words *is* the
content (no page fetch), and when the page refuses or gives a fragment, discovery falls
back to any feed text of ≥`MIN_CONTENT_WORDS` — Medium blogs and bot-protected
newsrooms both survive. `check-sources` applies the same rule, so its verdict matches. The
TLDR item — linked written headline, "(N minute read)", a 2–3 sentence descriptor, no
commentary, no source line — is the `feature` style with `show_source: false`,
`show_read_time: true` (new: `read_minutes` comes from the source text the blurb step
fetches) and a voice guide that returns empty intro and empty commentary; the subject
line is TLDR's comma list of the day's biggest stories. `newsletters/tech-links/voice.md`.

B2 first prework (2026-09-23, GPT-6 Luna): 2,121 items seen, 1,677 skipped as older than
96 h at admission (the freshness rule doing its job on a cold ledger), 444 → capped to
300, 3 thin drops. Zero rate-limit retries on Luna's 500k TPM. One mirror write of 297
rows died on a TLS error ("sslv3 alert bad record mac") — a single multi-megabyte
request. Fixed: mirror upserts go out in chunks of 50, each retried once.
Funnel: 297 classified → 115 off-industry → 59 non-news (26 other, 25 opinion, 4 review,
3 deal) → 123 news, scored (median 0.4, 36 ≥ 0.6, two at 0.8); spend **$0.33** for the
cold start. First preview: window dedup collapsed 28 of 123 into 15 clusters, every
keeper right (OpenAI's own post over four write-ups; the two-launch roundup kept apart
from Opus and GPT-6, as the dedup eval case demands); 21 stories across the five TLDR
sections; compile $0.009. Two things the voice guide asked for and the model ignored —
no intro, no commentary — became schema flags (`intro: false`, `commentary: false`),
because structure has to be data: the compile is told per story, and the parser blanks
whatever the model returns anyway. Preview after: no intro, no commentary, read-time
tags on all 17 written stories. Draft **243** on staging.wayrtoday.com (`Newsletter - B2`),
29 story hashes recorded, compile $0.008. Day one of B2 done on GPT-6 Luna end to end.

B3 first prework (2026-09-23, GPT-6 Luna): 489 items seen, 260 skipped as older than 240 h,
229 admitted, 39 thin drops (TV-station pages), 190 classified → **186 rejected by the
industry filter** → 3 news stories in ten days; $0.11, almost all of it the industry gate
saying no. Reading the rejections: the filter is right — crime, statewide politics, and
Community Impact's other Texas metros — with only about three misses (Austin Transit
Partnership's downtown lease, Georgetown's downtown retail partner, UT's quantum
research). **The expansion case is source-starved, not over-filtered**: the outlets that
carry Austin tech and business — ABJ / Austin Inno (403), Statesman (404), Built In,
CultureMap, Technical.ly (HTML), Austin Monitor and Silicon Hills (feeds dead for
~10 months) — have no usable feed, and Community Impact has no per-edition feeds. A
second probe found only KUT's business desk and UT's research feed to add. Two
conclusions: the `page` adapter is what a real local customer needs (recorded in the
plan as the next source type); and for the demo the scope is widened from "technology,
startups and business" to the civic-and-business beat Technically itself runs (city and
county decisions, development, transit, housing, university), with the criteria still
ranking tech and business first. The thin preview also exposed a rendering gap: a story
whose page could not be extracted shipped with no blurb at all — the feed summary now
stands in.
Re-run under the widened scope (state reset, +KUT business, +UT research): 180
classified → 120 off-scope → 12 non-news → **48 news**, scored median 0.2 with the
criteria still ranking tech and business first; $0.17. Preview and draft **245**:
"7 things to know" filled — Firefly's cleanroom, the Dog's Head development lawsuit,
UT's 207,000-sq-ft engineering building, ATP's $13.5M downtown lease, ERCOT's board
pay, Airbnb's $6.4M for St. Johns, a UT–Sandia recycling method — every item with a
blurb and a source link, no commentary, the editor's note up top; compile $0.004.
"Headlines of the week" stayed empty: nothing between 0.35 and 0.6 after the seven
featured took the top, which is the threshold pair to tune for a low-scoring beat.

What to look for, per case: **B1** — the top story is a genuine launch/rule, the
five quick hits differ from it, the deep dive got `show_source`, placeholders render as
editor blocks. **B2** — the Research and Engineering sections filled from category
routing rather than staying empty; Quick Links has 5–8 items; nothing has a blurb longer
than a sentence. **B3** — the candidate dump is the test: how many of the general-news
items the industry classifier dropped, and whether the survivors' scores separate
"Tesla expands Giga Texas" (≥0.7) from "city council approves budget" (≤0.4). If they do
not, tune the calibration block of `criteria.md` first, thresholds second.

### B1 findings — first prework, 2026-09-21

Cold start over 20 feeds: 2,476 items seen, 300 admitted (cap, newest first), ~8½ min.
Funnel: 106 dropped before classification (paywalls: Bloomberg 20, The Information 19;
plus rate-limit casualties, see below) → 194 classified → 68 off-industry → 71 non-news
(39 "opinion") → 55 news, 55 scored; 41 of those inside the 96 h window.

1. **Rate limits deleted stories.** OpenAI returned 429 (200k TPM on the nano tier) for
   much of the run. Two retry layers stacked — the SDK's own 2 retries inside our 3 —
   so each call thrashed ~9 times in ~5 s, inside the same exhausted minute, then gave
   up. Worse, a failed article was already recorded at `discovered` and admitted in the
   ledger, and the tracker treats "seen at any stage" as processed: **every 429 casualty
   was silently gone from the pool for good.** Fixed: 5 attempts with jittered backoff
   up to 60 s (`BaseAgent`), and a classification *failure* now releases the article
   (`FeedLedger.release`, `ArticleTracker.forget_discovered`) so the next run retries
   it. B1's state was repaired by hand (106 released).
2. **The classifiers are token-bound, not call-bound.** Requests were 3–11k tokens: full
   extracted article + 7–9k-char system prompts, three times per story. At 200k TPM that
   is ~50 calls/min whatever the concurrency. `CLASSIFICATION_MAX_CONTENT_CHARS=3000`
   (new, opt-in; the stored article keeps full content) roughly halves it; the B envs
   set it with `PIPELINE_CONCURRENCY=4`. The industry/news-type evals should be re-run
   with the cap before it goes into a prod env.
3. **Cold-start waste.** "Newest 300" reached back to Sep 8: 106 of 300 admitted items
   were already older than the 96 h freshness cap and 73 were classified anyway — about
   a third of the spend on stories the compile can never use. Fixed: discovery now
   skips items past `NEWSLETTER_MAX_STORY_AGE_HOURS` before admission (newsletter
   modes only; they stay unadmitted, so the ledger keeps them out of later deltas).
4. **Paywalled feeds are dead weight.** Bloomberg and The Information yielded 0 words
   for all 39 items. `wayr check-sources` now catches this before a run: it fetches
   each feed and extracts its newest items exactly as discovery would, and reports
   ok / blocked / stale / empty per feed — on B1's list it flagged exactly those two
   (13 words/item and 0). Run it on every new sources file. (Post-MVP: fall back to
   the feed summary the ledger already keeps.)
5. **Newsroom launches misrouted by the news-type classifier.** "GPT-6 Astra: The next
   generation in intelligence for work" (OpenAI News) → `review`; "Expanding AI access
   and cyber defense for federal…" → `deal`. The taxonomy was tuned on press outlets;
   first-party announcements read as product pages. The biggest story of the run was
   dropped. Prompt work for the news classifier (a first-party announcement is news),
   and evidence for the non-news routing item.
6. **Scores are compressed.** Max 0.70, median 0.50; 5 fresh stories ≥ 0.6 for 14
   feature-tier slots. "Gemini went rogue, hacked three companies, and Google hid it"
   at 0.60 is under its own calibration block. Step 2's dump decides whether it is the
   criteria wording or the model; the deferred importance eval is the real answer.

### B1 findings — first compile and draft, 2026-09-22

The dump showed 36 candidates scored 0.2–0.6 (18 at 0.5), and two structural bugs:

7. **Prework's dedup had been lost in the 429 storm — silently.** The window held five
   versions of the Gemini breakout and four of the US–China notification mechanism. The
   agent is fine (on the nine in isolation it kept one per event); the call failed and
   discovery logs that as a warning and continues. Fixed: the compile runs its own
   dedup over the window (`_dedup_window`, one call over tens of stories), keeps the
   best-scored, and flags losers `duplicate_of` in the pool so they leave every later
   window. On B1 it collapsed 9 of 36.
8. **Tier was a gate, not a preference.** Assignment matched tier strictly, so with 5
   featured stories 9 feature-tier slots sat empty and 22 roundup stories went
   unplaced. Fixed: a fill pass after strict placement lets leftovers take remaining
   room in schema order (category filter and `max` still apply); the section's style
   governs how a promoted story renders.

With both: 14 written + 6 link-only stories across 7 sections, subject "Grok 4.7 is
cheap, but still trails top models", Top Story / 5 quick hits / Deep Dive ×2 / two
placeholders (Tuesday) / Around the Horn ×6 / 6 bare links — a Neuron-shaped issue.
Draft **235** on staging.wayrtoday.com in the new `Newsletter - B1` category; run
recorded with 20 story hashes. Second run same day → `already_sent`. `--force` alone →
`empty` (the watermark moved to the draft; force lifts the guard, it does not rewind).
`--force --window-hours 96 --skip-publishing` → 20 excluded as shipped, 6 left, an
issue of 4 with nothing repeated.

### B1 against The Neuron's issue of the same day

Draft 235 next to the Neuron's 2026-09-22 issue sorted the gaps into three kinds:

- **Voice was the compile prompt's, not the customer's.** The prompt hardcoded "plain,
  direct, professional… no rhetorical questions… no 'welcome back'"; the Neuron opens
  "Welcome, humans. Did y'all see…?" and its lead runs ~350 words to "**Our take:**".
  Fixed: `NEWSLETTER_VOICE_FILE` (inline `NEWSLETTER_VOICE` → file → packaged
  `voice.default.md`, the old plain voice) replaces the tone block; the intro and
  commentary rules defer to it; the renderer's `prose` filter allows `**bold**` and
  paragraph breaks. `newsletters/ai-daily/voice.md` is written from that issue.
  Regenerated: "Welcome, humans. AI had one of those days where the future showed up
  early and immediately asked for admin access. Did y'all see what happened?" and a
  lead that goes distinction → Our take → what to watch without restating the blurb
  (the first draft of the guide made it restate; the blurb is printed above).
- **Schema had two sections wrong.** "Treats to Try" is a stories section (tool
  launches, one line + link, routed by a new `Tools & Products` category) and the
  Tuesday slot is a feature taken at an angle — both were placeholders. Fixed.
- **Selection: their lead was Amazon blocking Muse, which we scored 0.5 and shipped as
  a bare link.** Adding "fights over what agents are allowed to do" and "a story with a
  narrative" to `criteria.md` moved it to 0.7 (tied top) on a re-score; Gemini 0.6→0.7.
  The ceiling did not move — nothing reaches 0.8, eleven stories sit at 0.5 — so the
  scorer's range is the importance eval's question, not the criteria's. Two of their
  stories came from The Information / Bloomberg, the feeds we cannot read.
- **Beyond MVP, now with evidence:** the lead's outside voices are X posts and blogs
  synthesized into one story — the multi-source cluster plus X sourcing.

Still open for B1: the importance eval. The newsroom path is built as typed sources
(`origin=brand`, see the plan); migration `2026-09-22_source_origin.sql` must be applied
before the next prework or the mirror drops every discovery batch.

### B1 day 2 — 2026-09-23, first run with typed sources

Prework: 136 admitted (2,272 left unadmitted — the brand archives, skipped by the 96 h
rule) → 16 dropped → 120 classified → 45 news, ~6½ min, no 429 storm. The brand path
worked: 14 of 20 brand items classified `news`, the other five correctly `other`
(an employee profile, "5 companies using…", a principles post, a customer story);
"Introducing GPT-6 Sol and Luna" scored 0.8, the first score above 0.7. Compile: window
since draft 235 held 45 candidates, none shipped; window dedup collapsed 2; Treats to
Try filled for the first time (4); draft **236** on staging, 24 story hashes recorded.

9. **Discovery's dedup threw away the day's second launch.** Sixteen stories sat at
   `discovered` after prework: Anthropic's Opus 5.5 coverage ×4, GPT-6 Sol/Luna press
   ×3, Muse copycat/exploit ×3. Discovery's LLM dedup, run over the 136-item batch,
   grouped the whole "cheaper frontier models" wave as one event and brand-wins kept
   only OpenAI's post — Anthropic (a `page` source, no brand post of its own) vanished
   from the issue. On the same 14 titles in isolation the agent groups correctly (Opus,
   GPT-6, the two-company roundups, Muse milestone, Muse 0-day all separate); the
   defect is grouping at 136-batch scale and, more fundamentally, discarding coverage
   before anything has scored it. Fixed: discovery runs no LLM dedup in newsletter
   mode — the compile's window dedup (≈45 candidates, after scoring, origin-aware) is
   the one place same-event stories collapse. Cost: ~15% more classification calls.
   The 14-title batch is a dedup eval regression case; the 16 stories were released
   for the next prework. The Neuron led with exactly that price-war story. Moving
   dedup to the compile also moved the cross-batch check: the window dedup now
   passes the stories shipped within `DEDUP_WINDOW_DAYS` as the agent's cached
   members, so a second outlet's write-up of yesterday's story cannot ship today.
   That check has one trap, hit the same afternoon: with `--include-sent` the shipped
   stories are candidates *and* cached members, so each matched itself and the pool
   flagged 23 shipped stories as their own duplicates (repaired by hand). Fixed:
   shipped stories present in the window are left out of the cached list, and
   `mark_duplicates` refuses a story as its own keeper.

### Day 3 (2026-09-24): steady state, and the NYC demo

- **ai-daily, day 3** (first full day on GPT-6 Luna): 98 admitted, 2,193 seen-but-old
  left alone, 44 off-industry, 9 non-news → news scored; prework **$0.11**, compile
  $0.006; window dedup collapsed a five-way OpenAI cluster and dropped one write-up of
  a story that had already shipped; draft **246** (20 stories, 3rd issue, no repeats).
  Opening: "An AI voice that can reach into your work tools is a different beast from
  one that just chats. Would you hand it the follow-up, too? Here's what moved." — the
  voice guide's manner, nobody's lines. Section names made generic the same day.
- **nyc-local, first run** — the Austin demo re-onboarded for New York with one
  `wayr onboard` call: 12 checked feeds, 191 admitted, 189 classified → **88 news**
  (AlleyWatch alone supplies the startup beat Austin lacked), $0.23; window dedup
  collapsed 8 of 87 (the DoorDash $131.5M settlement ran in three outlets); draft
  **247**: DoorDash, Ackman's $400M brain-research pledge, Chamelio's $26M, Conduct
  AI's SoHo office, a $507M week led by Cyera, Hochul's AI "kill switch", plus eight
  headlines of the week. The same source gap as Austin on the trade side — Crain's,
  NYBJ, Built In, Technical.ly, Cornell Tech, NYC EDC — is the Technical.ly
  conversation.
- **tech-links, day 2 — hung for 2½ hours.** The log stopped right after "Starting
  WAYR Prework": `feedparser.parse(url)` fetches with **no timeout**, and one feed's
  connection simply never answered (Samsung's had timed out the day before). Fixed:
  the fetcher does the HTTP itself with `requests` (10 s connect / 30 s read, a real
  User-Agent, the conditional-GET validators as headers) and feedparser only parses
  the bytes; a slow or failing feed is recorded in the ledger as an error and the run
  moves on. Re-run: 13½ minutes, one feed recorded as a 30 s read timeout and skipped,
  297 admitted, 145 classified → 12 featured + 17 roundup, draft **248**; prework
  $0.14, compile $0.006.

## C — new newsletter, one source (someone's own stories)

A single feed — a blog, a company newsroom. Two settings matter, because the pipeline
is news-centric and a personal feed usually is not:

- **`ENABLE_CLASSIFICATION=false`.** The industry filter and the news/non-news classifier
  would reject tutorials, opinion pieces and updates — which is most of what a single
  author publishes. Off, everything discovered is treated as news, has no category
  (fine: the default schema has no category filters), and still gets an importance
  score. Alternatively keep classification on with a permissive `INDUSTRY`, but expect
  drops.
- **`NEWSLETTER_MAX_STORY_AGE_HOURS=168`** (or more). Freshness is by publish date; a
  low-volume source may have nothing in 36 hours, and a week-old post from the author's
  own blog is fine to feature.

```bash
# .env.scenario-c
PIPELINE_MODE=newsletter
BEEHIIV_PUBLICATION_ID=pub_...            # can be the same publication as B
INDUSTRY=...                               # still required; only matters if classification is on
INDUSTRY_CATEGORIES=General
RSS_FEEDS=https://their-site.example/feed
ENABLE_CLASSIFICATION=false
NEWSLETTER_MAX_STORY_AGE_HOURS=168
NEWSLETTER_MIN_STORIES=3                   # a small source; the default floor of 5 may exceed the pool

ARTICLE_CACHE_FILE=data/scenario-c/processed_articles.json
WP_TAXONOMY_CACHE_FILE=data/scenario-c/wp_taxonomy.json
FEED_LEDGER_FILE=data/scenario-c/feed_ledger.json
NEWSLETTER_RUNS_FILE=data/scenario-c/newsletter_runs.json
```

What to look for: the cold start pulls the feed's whole window (typically 10–50 posts);
in steady state a single source yields 0–2 posts a day, so `empty` is a normal outcome
and `--window-hours` is the way to demo it; the importance criteria are what make a
single-author digest read as curated rather than as "every post" — write a
`NEWSLETTER_IMPORTANCE_CRITERIA_FILE` for this one.

---

## Reading the results

- `data/<dir>/newsletter_preview_<date>.html` — open in a browser; check each section's
  link anchor matches its style.
- `data/<dir>/newsletter_candidates_<date>.csv` — sort by score; set
  `NEWSLETTER_FEATURE_THRESHOLD` / `NEWSLETTER_ROUNDUP_THRESHOLD` where the tiers look
  editorially right, then re-run the preview.
- `data/<dir>/newsletter_runs.json` — after step 4, one `success` row with the Beehiiv
  post id and `web_url`. A second run the same day must say `already_sent`.
- `data/<dir>/feed_ledger.json` — `feeds[*].last_status` should be `200` on the first
  run and `304` on the second for feeds that support conditional GET.
