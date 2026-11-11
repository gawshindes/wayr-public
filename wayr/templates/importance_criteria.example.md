<!--
EXAMPLE reader criteria — copy this file, edit it for the newsletter, and point
NEWSLETTER_IMPORTANCE_CRITERIA_FILE at your copy. Everything below the comment
is pasted verbatim into the ImportanceAgent's prompt, where it outranks the
prompt's general guidance. HTML comments are stripped.

The newsletter described here is fictional. The *shape* is what matters:
who reads it, what they act on, what to prioritize and skip, and — most
useful of all — calibration examples that pin the score anchors to concrete
stories. When onboarding is automated, the calibration section is the part
we derive from a customer's own published issues.
-->

# Ship Notes — reader criteria

## Who reads this

Engineering leaders — VPs of Engineering, CTOs, and senior staff engineers — at
venture-backed software startups between Series A and Series C, mostly in the
US and Western Europe, teams of 10 to 150 engineers. They read us at the start
of the day, on a phone, in under five minutes.

## What they need from us

To walk into their day knowing what changed in the tools, platforms and rules
their teams depend on, and whether they need to do anything about it. They are
not researchers and not investors: they want operational relevance, not
completeness.

## Prioritize

- **Platform and infrastructure changes** they build on: cloud providers, CI/CD,
  databases, observability, major frameworks and languages. Pricing changes,
  deprecations and breaking changes rank especially high — they force work.
- **Developer-tool and AI-coding releases** that change how teams write, review
  or ship code. A real capability step, not a version bump.
- **Security incidents and CVEs** in widely used dependencies or platforms. If
  a reader's team might have to patch or rotate something this week, it leads.
- **Hiring-market and compensation signals** with data behind them, and major
  layoffs or hiring freezes at companies our readers benchmark against.
- **Regulation with engineering consequences** — data residency, AI liability,
  export controls on models or hardware — but only once it is decided or in
  force, not while it is being debated.
- **Acquisitions and shutdowns of tools our readers use.** A vendor being bought
  or wound down is an operational event for its customers.

## Deprioritize

- Funding rounds, unless the company is a tool a large share of readers use and
  the round changes its trajectory. A Series B at a company they have never
  heard of is not news to them.
- Consumer product launches, hardware reviews, gadget news.
- Executive hires and departures below CTO level, or at companies readers do
  not depend on.
- Partnership announcements with no shipped result.
- Opinion pieces and think-pieces, even good ones — we may link them in the
  roundup, but they are never lead stories.
- Anything primarily about crypto, ad-tech, or consumer social.

## House rules

- We never lead with a funding round.
- Two stories about the same event on the same day: the one from the primary
  source (the vendor's own announcement, the CVE, the filing) outranks the
  coverage of it.
- If a story is important but the reader can do nothing about it, it belongs in
  the roundup, not the body.

## Calibration examples

These pin the score anchors to the kind of story each one means *for these
readers*. Score against these, not against general newsworthiness.

- **9–10** — GitHub changes Actions pricing for private repos, effective next
  month. A critical RCE in a logging library present in most JVM stacks. AWS
  deprecates an instance family readers are running production on.
- **7–8** — A major AI coding assistant ships agentic multi-file edits to all
  paid tiers. Postgres 18 is released with a change to default connection
  handling. A widely used observability vendor is acquired by a larger one.
- **5–6** — A popular framework publishes a minor release with a notable
  performance improvement. A well-known startup announces a hiring freeze. A
  new benchmark shows one model family clearly ahead for code generation.
- **3–4** — A developer-tools company raises a Series B. A cloud provider opens
  a new region readers are unlikely to use. A survey on remote-work preferences
  with a small sample.
- **0–2** — A consumer phone launch. A VP of Marketing hire at a mid-size SaaS
  company. A partnership press release with no product attached.
