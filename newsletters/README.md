# Demo newsletters

Each folder is one newsletter, fully described by five reviewable files and nothing else:

| file | what it is | who edits it |
|---|---|---|
| `description.md` | the brief: name, reader, cadence, where drafts land | the customer |
| `sources.txt` | one source per line: `URL [type=rss\|page] [origin=press\|brand]` | onboarding, then `./demo.sh <name> check` |
| `criteria.md` | what matters to these readers — the importance agent's prompt | the customer |
| `sections.yaml` | the structure: sections, item styles, tiers, category routing, `intro`/`commentary` flags | onboarding |
| `voice.md` | how it sounds (optional; the packaged default is plain and professional) | the customer |
| `env.overrides` | the settings that wire those in and isolate state; **no secrets** | onboarding |

Secrets live once, in `.env.demo.base` (gitignored): API keys, the staging WordPress
credentials, Supabase. `demo.sh` appends a demo's `env.overrides` to it.

## In the public release

Only `example/` ships publicly: a weekday tech digest made with `wayr onboard example
--from tech-links`, the starting point for your own. The demo newsletters below and the
production `wayrtoday/` are the author's own editorial files and are excluded from the
export by `.gitattributes` (`export-ignore`). `./demo.sh example check`, `prework`,
`preview` work on it like on any other.

## The demos

| name | shape | shows |
|---|---|---|
| `wayr-daily` | daily digest from the WAYR news pipeline's own pool | a customer who already runs the news pipeline — no second discovery; `import` instead of `prework` |
| `ai-daily` | conversational AI daily: lead, quick hits, deep dive, one-liners, treats | a voice guide that describes a manner (never lines), placeholders the editor owns, brand sources |
| `tech-links` | terse link digest: five fixed sections, "(N minute read)", no intro, no commentary | many sources, category routing, structure as data (`intro: false`, `commentary: false`) |
| `austin-local` | weekly local: "7 things to know" + headlines of the week | general local-news feeds filtered to a city beat; the case for the `page` adapter |
| `own-site` | weekly best-of from one first-party feed | the smallest setup: one source, classification off, criteria alone curate |
| **`wayrtoday`** | **production** — wayrtoday.com's daily digest, published (not drafted) to cms.wayrtoday.com by the Modal deployment in the `wayr` workspace, then emailed to the site's confirmed subscribers via Resend (`NEWSLETTER_MAILER=resend-direct`, configured in `.env.usa`) | the same folder shape runs the real site; `demo.sh wayrtoday` runs it locally against the staging base with the mailer off |
| `nyc-local` | weekly city, the Austin shape re-onboarded for New York | `wayr onboard nyc-local --from austin-local --answers …` end to end; 12 checked feeds; a voice drawn from the city's own newsrooms |

All five ship drafts to **staging.wayrtoday.com** under their own `Newsletter - …`
category. Nothing is ever published or sent.

## Running one

```bash
./demo.sh <name> env        # assemble .env.demo-<name>, show the resolved config
./demo.sh <name> check      # do the feeds deliver stories? (any new or changed sources file)
./demo.sh <name> prework    # discover → classify → score into the pool   (wayr-daily: `import`)
./demo.sh <name> plan       # what the next issue would take; nothing runs
./demo.sh <name> dump       # scored candidates as CSV — tune criteria/thresholds from it
./demo.sh <name> preview    # HTML under data/<name>/; nothing pushed or recorded
./demo.sh <name> draft      # compile, push a draft to staging, record the run
./demo.sh <name> day        # prework (or import) + draft, in one go
./demo.sh <name> reset      # cold start (never for wayr-daily)
```

Every run ends with a *Spend this run* line per agent. Typical: a 300-story cold start
≈ $0.35, a steady-state day ≈ $0.10, a compile ≈ $0.01 (GPT-6 Luna, September 2026).

## Reading a run

- Prework log: `Skipped N item(s) older than …` (freshness at admission), the
  classification funnel, `Using feed text …` (page refused, feed stood in), the spend.
- Compile log: `Window dedup: collapsed N of M` and each `Window duplicate: … → kept …`,
  `Selected from N candidates`, per-section counts, `Unplaced: N` (the schema's caps
  working, not a leak), the draft id.
- `data/<name>/newsletter_candidates_<date>.csv` after `dump`: the score spread. If
  everything sits at 0.5, the criteria's calibration block is the lever.

## Onboarding a new one

```bash
poetry run wayr onboard fintech-weekly --from tech-links          # prompts for the rest
poetry run wayr onboard fintech-weekly --from tech-links --answers answers.json   # what a form would post
```

`onboard` writes the folder from a few answers — name, one line, readers, cadence,
feed URLs, what matters most / less — starting from the demo closest in shape: its
`sections.yaml` and `voice.md` are copied, `criteria.md` is written in the customer's own
words, `env.overrides` gets the paths, category and cadence. Everything is plain text to
review before the first run. Or copy the closest folder by hand and edit the five files. `scripts/find_feeds.py`
turns a domain list or a set of issue URLs into a validated `sources.txt`;
`scripts/describe_newsletter.py` reads an existing newsletter's issues and reports the
structure to encode in `sections.yaml`. Then `./demo.sh <name> check`, `prework`, `dump`,
`preview`, `draft`.
