# 🚀 Modal Deployment Guide for WAYR

This guide walks you through deploying WAYR to Modal.com in **10-15 minutes**.

## Prerequisites

- Python 3.11+
- Poetry installed
- Modal account (free to start)

---

## Step 1: Install Modal CLI (1 minute)

```bash
pip install modal
```

Or with pipx (recommended):
```bash
pipx install modal
```

---

## Step 2: Authenticate with Modal (1 minute)

```bash
modal token new
```

This will:
1. Open your browser
2. Ask you to sign up/login to Modal
3. Create an authentication token
4. Save it to your machine

**You'll get $30 in free credits automatically!** 🎉

---

## Step 3: Create Modal Secret (3 minutes)

Modal uses "secrets" to securely store environment variables. We'll create one secret with all your credentials.

Create a Modal secret named **`wayr-secrets-<env>`** (e.g. `wayr-secrets-prod`) with all variables from `.env.example`. For a newsletter deployment `scripts/modal_secret.py` builds it from the region env file and the newsletter folder; otherwise use the [Modal dashboard](https://modal.com/secrets) or CLI.

---

## Step 4: Test Connection (2 minutes)

Before deploying, let's test that everything works:

```bash
modal run modal_deploy.py::test_connections
```

Expected output:
```
Testing WAYR connections...
✓ WordPress connection successful
✓ OpenAI connection successful
✓ Anthropic connection successful
✅ All connections successful!
```

If any connection fails, double-check your secrets!

---

## Step 5: Test Preview Mode (3 minutes)

Run the pipeline in preview mode (no publishing):

```bash
modal run modal_deploy.py::run_wayr_preview
```

This will:
1. Discover articles from RSS feeds
2. Classify them
3. Generate AI articles
4. Proofread with Claude
5. **NOT publish** (preview only)

Expected output:
```
Starting WAYR Pipeline
...
✓ Discovered 5 new articles
✓ Classified 5 articles
✓ Generated 5 articles
✓ Proofread 5 articles
Publishing skipped (preview mode)
```

---

## Step 6: Deploy to Production (1 minute)

If the preview works, deploy the scheduled version:

```bash
modal deploy modal_deploy.py
```

This will:
1. Build the container image (~2 minutes first time)
2. Upload your code
3. Create the scheduled function
4. Start running every 2 hours automatically!

Expected output:
```
✓ Created objects.
├── 🔨 Created mount ~/wayr-platform
├── 🔨 Created function run_wayr_pipeline
└── 🔨 Created schedule (runs every 2 hours)

View logs: https://modal.com/apps/...
```

---

## Step 7: Monitor Your Deployment

### View Logs

```bash
modal app logs wayr-platform
```

Or visit: https://modal.com/apps

### Manual Run (Trigger Now)

```bash
modal run modal_deploy.py
```

Or click **"Run Now"** in the Modal dashboard.

### Stop the Schedule

```bash
modal app stop wayr-platform
```

---

## 🎉 You're Live!

Your WAYR pipeline is now running every 2 hours on Modal:
- **12am, 2am, 4am, 6am, 8am, 10am, 12pm, 2pm, 4pm, 6pm, 8pm, 10pm (UTC)**

### Next Steps:

1. **Monitor first few runs** - Check Modal dashboard for logs
2. **Verify WordPress** - Check your site for new articles
3. **Adjust schedule if needed** - Edit `modal_deploy.py` and redeploy

---

## Newsletter Mode

A newsletter deployment schedules two functions instead of the per-story pipeline:

| Function | Default cron | Does |
|---|---|---|
| `run_wayr_prework` | `0 */2 * * *` | discover → dedup → extract → classify → score into the pool |
| `run_wayr_newsletter` | `30 6 * * *` | window → select → summarize → compile → render → **Beehiiv draft** |

```bash
WAYR_PIPELINE_MODE=newsletter ./deploy.sh prod deploy
# or, moving the daily compile (keep the :30 so it never coincides with prework):
WAYR_PIPELINE_MODE=newsletter WAYR_NEWSLETTER_CRON="30 17 * * *" ./deploy.sh prod deploy
```

**The mode is decided at deploy time**, by `WAYR_PIPELINE_MODE`, because schedules are
fixed on deploy while Modal secrets are only readable inside a running function. The
deploy injects that mode into every function as `PIPELINE_MODE` *after* your secret, so
a `PIPELINE_MODE` inside the Modal secret is ignored. Your secret needs, in addition to
the model keys and feeds: `BEEHIIV_API_KEY`, `BEEHIIV_PUBLICATION_ID`, `INDUSTRY`,
`INDUSTRY_CATEGORIES`, and optionally `NEWSLETTER_IMPORTANCE_CRITERIA_FILE` /
`NEWSLETTER_SECTIONS_FILE` (paths inside the image or the `/data` volume).

State on the volume is owned by exactly one function, so the two can run concurrently:
prework writes `feed_ledger.json` and `processed_articles.json`; the compile job writes
only `newsletter_runs.json` and reads the pool. The `/data/` paths are injected as
defaults; your secret can override them. The watermark advances only when the draft push
succeeds — a failed push is recorded and the next run re-reads the same window.

Manual runs:

```bash
modal run modal_deploy.py::run_wayr_newsletter --skip-publishing   # preview → /data/newsletter_preview_<date>.html
modal run modal_deploy.py::run_wayr_newsletter --with-prework      # one-shot: prework then compile then draft
modal run modal_deploy.py::run_wayr_prework                        # prework only
modal run modal_deploy.py::test_connections                        # Beehiiv + model keys
```

## Cutover: wayrtoday.com from per-story news to a daily digest

wayrtoday.com runs in **newsletter mode only**: the per-story news app is stopped and
nothing posts individual stories to the site any more. Each day the compile publishes one
digest post to cms.wayrtoday.com and, once Resend is set up, emails the same issue to the
subscriber list.

The production newsletter is a folder like any demo — `newsletters/wayrtoday/` (22
checked sources, criteria, sections, voice, `NEWSLETTER_DELIVERY=publish`). The Modal
secret is that folder merged with `.env.usa`, minus the local state paths and
`PIPELINE_MODE` (see `scripts/modal_secret.py`).

Two Modal workspaces are involved. The old news app lives in the previous, personal
workspace (app `wayr-platform-prod`, secret `wayr-secrets`, volume `wayr-data-dev`) and is
only stopped. The newsletter deploys into the **`wayr`** workspace, which starts clean:
secret `wayr-secrets-prod`, volume `wayr-data-prod` (created on first deploy, so the first
prework fills an empty pool — expect a ~300-story cold start, about $0.35).

```bash
# 1. stop the per-story news app in the old workspace — it posts to cms.wayrtoday.com every two hours
MODAL_PROFILE=<old-profile> poetry run modal app stop wayr-platform-prod

# 2. in the wayr workspace: dry run, then upload the secret
poetry run modal profile activate wayr
poetry run python scripts/modal_secret.py --env-file .env.usa --newsletter wayrtoday            # keys (redacted) + adjustments
poetry run python scripts/modal_secret.py --env-file .env.usa --newsletter wayrtoday --apply    # creates wayr-secrets-prod
#    add --set NEWSLETTER_DELIVERY=draft for the first issue: WordPress draft + email to NEWSLETTER_TEST_RECIPIENTS only

# 3. deploy the newsletter app: prework every 2h, compile daily (UTC; pick the hour)
WAYR_PIPELINE_MODE=newsletter WAYR_NEWSLETTER_CRON="30 12 * * *" ./deploy.sh prod deploy

# 4. connections, then the first issue by hand: as a preview, then for real
WAYR_ENV=prod WAYR_PIPELINE_MODE=newsletter poetry run modal run modal_deploy.py::test_connections
WAYR_ENV=prod WAYR_PIPELINE_MODE=newsletter poetry run modal run modal_deploy.py::run_wayr_newsletter --with-prework --skip-publishing
WAYR_ENV=prod WAYR_PIPELINE_MODE=newsletter poetry run modal run modal_deploy.py::run_wayr_newsletter
```

With `NEWSLETTER_DELIVERY=publish` the compile **publishes** the post in the `Newsletter`
category — there is no human step; set it to `draft` in `env.overrides` and re-upload the
secret to put one back. `feed_ledger.json`, `processed_articles.json` and
`newsletter_runs.json` appear on the volume after the first prework and compile.

### Email: the issue to subscribers via Resend

The publisher puts the issue on the web; a **mailer** (`NEWSLETTER_MAILER`, see
`wayr/mailers/`) sends the same HTML — wrapped with a read-on-the-web link and an
unsubscribe link — to the list. A send failure never fails the run (the issue is on the
web and must not ship twice); it is recorded on the run as `mail_error` and the CLI exits
non-zero. Two Resend adapters:

- **`resend-direct`** (what wayrtoday uses): the list is **ours** — the site's
  `subscribers` table in the production Supabase project (double opt-in, per-row
  `unsubscribe_token`, row-level security; the site's own repo defines it). Before every
  send the mailer reads `email, unsubscribe_token where status = 'confirmed'` with the
  service-role key, so recent unsubscribes are excluded, and sends one email per
  subscriber through Resend's batch endpoint (100 per request, an `Idempotency-Key` per
  batch, `retry-after` honoured). Every email carries that subscriber's own link,
  `NEWSLETTER_UNSUBSCRIBE_URL` with `{token}` filled in, in the footer and in a
  `List-Unsubscribe` header; the site's `/newsletter/unsubscribe` page handles it. A
  "Sending access" Resend key is enough. Email addresses are never logged.
  `NEWSLETTER_DELIVERY=publish` sends to the confirmed list; `draft` sends the issue to
  `NEWSLETTER_TEST_RECIPIENTS` only (your inbox), which is how to see an issue in a mail
  client before trusting the automation.
- **`resend`**: the list lives **at Resend** — one Broadcast to a Segment, Resend's own
  `{{{RESEND_UNSUBSCRIBE_URL}}}`; needs a "Full access" key and `RESEND_SEGMENT_ID`;
  `draft` leaves the broadcast in the Resend dashboard. For a newsletter without its own
  signup system.

Settings for wayrtoday, all in `.env.usa` (the overrides never carry the mailer, so a
local `demo.sh wayrtoday` run has none and only publishes to staging):

```
NEWSLETTER_MAILER=resend-direct
RESEND_API_KEY=re_...                                   # Sending access is enough
NEWSLETTER_FROM=WAYR Today <daily@wayr.today>           # domain must be verified at Resend
NEWSLETTER_REPLY_TO=...                                 # optional
NEWSLETTER_UNSUBSCRIBE_URL=https://wayrtoday.com/newsletter/unsubscribe?token={token}
NEWSLETTER_TEST_RECIPIENTS=you@example.com              # where a draft delivery goes
SUPABASE_URL / SUPABASE_SECRET_KEY                # already there: the pipeline mirror
```

`wayr test` checks the subscribers table is reachable (and prints the confirmed count),
and the sender's domain when the key can list domains (a sending-only key cannot; verify
`wayr.today` reads *verified* in the dashboard yourself). Apply the Supabase migration
`scripts/migrations/2026-11-10_newsletter_run_mail.sql` so the run mirror can store
`mail_id`/`mail_status`/`mail_error`. Then send one issue with `NEWSLETTER_DELIVERY=draft`
to your inbox before switching to `publish`. Resend's default limit is 10 requests/second
per team; a 10,000-subscriber issue is 100 requests.

Not done yet, on the site's side: one-click unsubscribe (`List-Unsubscribe-Post`), which
Gmail and Yahoo want from bulk senders above ~5,000 emails/day; the unsubscribe page needs
to accept that POST.

## Schedule Customization

Want to change the schedule? Edit `modal_deploy.py`:

```python
# Every 2 hours (current)
schedule=modal.Cron("0 */2 * * *")

# Every 3 hours
schedule=modal.Cron("0 */3 * * *")

# Every hour
schedule=modal.Cron("0 * * * *")

# Every 30 minutes
schedule=modal.Cron("*/30 * * * *")

# Specific times (e.g., 8am, 12pm, 6pm UTC)
schedule=modal.Cron("0 8,12,18 * * *")
```

After changing, redeploy:
```bash
modal deploy modal_deploy.py
```

---

## Troubleshooting

### "Secret not found"
Make sure the secret `wayr-secrets-<env>` exists in the workspace your Modal profile points at (`modal profile current`).

### "Import error"
Make sure all dependencies are in `pyproject.toml`. Modal builds from your Poetry config.

### "Pipeline takes too long"
Increase timeout in `modal_deploy.py`:
```python
timeout=1800,  # 30 minutes
```

### "Out of credits"
Check usage at https://modal.com/settings/billing

---

## Cost Monitoring

View your Modal usage:
```bash
modal app list
```

Or visit: https://modal.com/settings/billing

Expected cost: **$5-10/month** after free credits

---

## Support

- **Modal Docs**: https://modal.com/docs
- **Modal Discord**: https://discord.gg/modal
- **WAYR Issues**: Check logs with `modal app logs wayr-platform`
