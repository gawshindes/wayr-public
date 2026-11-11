# WAYR Deployment Quick Reference

## deploy.sh Usage

```bash
./deploy.sh [environment] [action]
```

### Environments

| Env | Secret | Volume | Notes |
|-----|--------|--------|-------|
| `prod` | `wayr-secrets` | `wayr-data-dev` | US (wayr.today) |
| `india` | `wayr-secrets-india` | `wayr-data-india` | India region |

### Actions

| Action | Description | Command |
|--------|-------------|---------|
| `deploy` | Deploy scheduled app (runs every 2h) | `./deploy.sh prod deploy` |
| `run` | Trigger one-time manual run | `./deploy.sh prod run` |
| `preview` | Test locally, no publish | `./deploy.sh prod preview` |
| `test` | Test API connections | `./deploy.sh prod test` |
| `logs` | View real-time logs | `./deploy.sh prod logs` |
| `stop` | Stop scheduled runs | `./deploy.sh prod stop` |

> [!NOTE]
> LinkedIn posting is built into the main pipeline via `SocialMediaAgent` — no separate deployment needed.

---

## Modal Secrets

### US Secrets (`wayr-secrets`)

```bash
modal secret update wayr-secrets \
  LINKEDIN_ACCESS_TOKEN="your-access-token" \
  LINKEDIN_REFRESH_TOKEN="your-refresh-token" \
  LINKEDIN_CLIENT_ID="your-client-id" \
  LINKEDIN_CLIENT_SECRET="your-client-secret" \
  LINKEDIN_ORGANIZATION_URN="urn:li:organization:YOUR_ORG_ID" \
  MODEL_SUMMARY="openai:gpt-4o-mini"
```

### India Secrets (`wayr-secrets-india`)

Same LinkedIn token, different org URN:

```bash
modal secret update wayr-secrets-india \
  LINKEDIN_ORGANIZATION_URN="urn:li:organization:YOUR_INDIA_ORG_ID" \
  LINKEDIN_ACCESS_TOKEN="your-access-token" \
  LINKEDIN_REFRESH_TOKEN="your-refresh-token" \
  LINKEDIN_CLIENT_ID="your-client-id" \
  LINKEDIN_CLIENT_SECRET="your-client-secret"
```

---

## Newsletter Mode (`WAYR_PIPELINE_MODE=newsletter`)

Deploy-time env vars: `WAYR_PIPELINE_MODE=newsletter`, optional `WAYR_PREWORK_CRON`,
`WAYR_NEWSLETTER_CRON` (default `30 6 * * *`; keep the `:30`).

Extra secret keys (same secret as the region): `BEEHIIV_API_KEY`, `BEEHIIV_PUBLICATION_ID`,
`INDUSTRY`, `INDUSTRY_CATEGORIES`; optional `MODEL_IMPORTANCE`, `MODEL_NEWSLETTER`,
`NEWSLETTER_IMPORTANCE_CRITERIA_FILE`, `NEWSLETTER_SECTIONS_FILE`, `NEWSLETTER_FEATURE_THRESHOLD`,
`NEWSLETTER_ROUNDUP_THRESHOLD`. `WORDPRESS_*` is not required. See MODAL_DEPLOYMENT.md.

```bash
WAYR_PIPELINE_MODE=newsletter ./deploy.sh prod deploy
WAYR_PIPELINE_MODE=newsletter ./deploy.sh prod run      # one-shot on Modal
WAYR_PIPELINE_MODE=newsletter ./deploy.sh prod test     # Beehiiv + model keys
```

## Common Workflows

### Full Deployment

```bash
./deploy.sh prod test      # Test connections
./deploy.sh prod deploy    # Deploy US
./deploy.sh india deploy   # Deploy India
./deploy.sh prod logs      # Check logs
```

### Token Refresh (Every 60 days)

Token auto-refreshes on 401, but if refresh token expires (~1 year):

```bash
python3 scripts/get_linkedin_token.py

modal secret update wayr-secrets \
  LINKEDIN_ACCESS_TOKEN="new_token" \
  LINKEDIN_REFRESH_TOKEN="new_refresh_token"
```
