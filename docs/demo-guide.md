# Newsletter demo guide

*Written for whoever runs a demo: what the product does today, the five ready-made
newsletters, and the commands. Nothing here sends email or publishes; every demo ends
in a draft on the staging WordPress.*

## What the newsletter product does now

**Research, continuously.** A prework job fetches every configured source (RSS today;
listing pages are declared in the sources file and reserved for the next adapter),
remembers every item it has ever seen so it only pays for the delta, skips items already
too old for the next issue, extracts each article (or uses the text the feed itself
carries when a page refuses), and drops the thin ones.

**Curate, in three steps.** An industry filter keeps the beat (a city, an industry, a
topic); a news filter removes deals, opinion, reviews and reports; a category classifier
routes what is left. Then an importance agent scores each story 0–1 against the
newsletter's own criteria — a plain-language file the customer writes about their
readers — and the score, not a fixed count, decides what gets in.

**Compile, on the newsletter's cadence.** The compile job takes the window since the
last issue, collapses same-event coverage (a company's own post wins the cluster, so the
issue links the primary source), selects by thresholds into two tiers, assigns stories to
a section schema (item styles, category routing, weekday placeholders, read-time tags),
writes a factual blurb per story from the source, and makes one editorial call — subject
line, opening, headlines, commentary — in the newsletter's own voice guide. It renders,
pushes a **draft**, and records the run so no story ever ships twice.

**Sources are typed.** `URL [type=rss|page] [origin=press|brand]`. A brand source (a
company's own newsroom) gets a first-party hint at the news filter — the title is the
announcement — and wins dedup clusters.

**Everything is a reviewable file.** A newsletter is a folder of six: description,
sources, criteria, sections, voice, env. Secrets live once, elsewhere. Onboarding writes
the folder from a few answers; `demo.sh` runs it step by step; every run ends with a
per-agent token and dollar line.

**What it does not do yet.** Publish or email from a demo (drafts only, by design —
see "full auto" below for the production path); read listing pages or X; keep several sources per story
("also: Outlet, Outlet"); show an editor a story picker.

## The demos

| name | shape | drafts to | shows |
|---|---|---|---|
| `wayr-daily` | daily digest from the WAYR news pipeline's own pool | `Newsletter - WAYR` | a customer who already runs the news pipeline: `import` instead of `prework`, lazy scoring of the pool, the missed-schedule preflight |
| `ai-daily` | conversational AI daily: lead, quick hits, deep dive, one-liners, treats | `Newsletter - AI Daily` | a voice guide that describes a manner (never lines), editor-owned placeholders, brand sources beside the press |
| `tech-links` | terse link digest: five fixed sections, "(N minute read)", no intro, no commentary | `Newsletter - Tech Links` | 31 sources, category routing, structure as data (`intro: false`, `commentary: false`) |
| `austin-local` | weekly city: "7 things to know" + headlines of the week | `Newsletter - Austin Local` | general local-news feeds filtered to a city beat; the honest case for the listing-page adapter |
| `own-site` | weekly best-of from one first-party feed | `Newsletter - Own Site` | the smallest setup: one source, classification off, criteria alone curate |
| `nyc-local` | weekly city, same shape as Austin: "7 things to know" + headlines of the week | `Newsletter - NYC Local` | the Austin demo re-onboarded for New York in one command — 12 checked feeds (AlleyWatch, THE CITY, Gothamist, amNY, Hell Gate, Observer, the Brooklyn papers, the real-estate trades), a voice drawn from the city's own newsrooms |

Measured on 2026-09-23 (GPT-6 Luna): a 300-story cold start ≈ $0.35, a steady-state
day ≈ $0.10, a compile ≈ $0.01.

## Commands

```bash
# once: secrets and shared settings (gitignored)
cp .env.demo.base.example .env.demo.base    # or write it: API keys, staging WordPress, Supabase

./demo.sh <name> env        # assemble .env.demo-<name>; show the resolved config
./demo.sh <name> check      # do the feeds deliver stories? (run on any new/changed sources file)
./demo.sh <name> prework    # discover → classify → score into the pool   (wayr-daily: import)
./demo.sh <name> plan       # what the next issue would take; nothing runs, nothing recorded
./demo.sh <name> dump       # scored candidates as CSV — tune criteria/thresholds from it
./demo.sh <name> preview    # the issue as HTML under data/<name>/; nothing pushed or recorded
./demo.sh <name> draft      # compile, push a draft to staging, record the run
./demo.sh <name> day        # prework (or import) then draft
./demo.sh <name> reset      # cold start (never for wayr-daily)
```

A demo from scratch, in five minutes:

```bash
poetry run wayr onboard fintech-weekly --from tech-links   # answers → newsletters/fintech-weekly/
./demo.sh fintech-weekly check
./demo.sh fintech-weekly prework
./demo.sh fintech-weekly preview
```

## What to show, per demo

- **wayr-daily**: `plan` first — it shows the pool, how much is unscored, and the
  missed-schedule guard. Then `preview`: the lazy-scoring line, then an issue.
- **ai-daily**: the voice. Open the draft: an opening hook, written headlines, a lead
  that argues, placeholders the editor fills. Then `newsletters/ai-daily/voice.md` — a
  page of prose, no lines.
- **tech-links**: the structure. Five sections, read-time tags, no editorial voice —
  and `sections.yaml`, where all of that is data.
- **austin-local**: the filter. `dump` shows 180 local stories scored, most low; the
  seven that made it are the city's business week. Then `sources.txt`: the outlets that
  could not be fed yet.
- **own-site**: the floor. One feed, `prework` in seconds for under a cent, a digest.
- **nyc-local**: onboarding. `newsletters/nyc-local/` came from one `wayr onboard`
  call with the Austin demo as the starting point and a JSON of answers — the shape a
  form would post. Then `check`, `prework`, `draft`, same as any other.

## Full auto

Every demo stops at a draft on purpose. Going live is a delivery setting, not a
different pipeline: `NEWSLETTER_DELIVERY=draft|publish` (default `draft`).

- **WordPress**: `publish` creates the post as published instead of draft. Available now.
- **Beehiiv**: post creation is the Send API (paid Max/Enterprise; every trial excluded).
  Whether a created post can be confirmed and sent — audience, schedule — is verified
  against the plan once one is in place; until then the adapter creates drafts.
- **Email to subscribers**: a mailer (`NEWSLETTER_MAILER=resend-direct` or `resend`) sends
  the published issue — to your own confirmed-subscriber table through Resend's batch
  endpoint, or as a Resend Broadcast to a Segment. Under `draft` it goes to your test inbox
  (or stays a dashboard draft); under `publish` to the list. No demo sets one; production
  wayrtoday does. See
  [MODAL_DEPLOYMENT.md](MODAL_DEPLOYMENT.md#email-the-issue-to-subscribers-via-resend).

Scheduling is Modal's: `WAYR_PIPELINE_MODE=newsletter` deploys prework on its cron and
the compile on `NEWSLETTER_CRON`; the folder's `env.overrides` becomes that app's
secret. See [MODAL_DEPLOYMENT.md](MODAL_DEPLOYMENT.md#newsletter-mode).

## Where the details are

- [e2e-scenarios.md](e2e-scenarios.md) — every run, every finding, in order.
- [newsletter-mode-plan.md](newsletter-mode-plan.md) — the design, the decisions, what
  comes next.
- [newsletters/README.md](../newsletters/README.md) — the folder contract and onboarding.
