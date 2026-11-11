"""
Modal deployment for WAYR (What Are You Reading Today) platform.

One deployment is one of two shapes, chosen at DEPLOY time:

  news        run_wayr_pipeline on WAYR_PIPELINE_CRON     (default: every 2 hours)
  newsletter  run_wayr_prework  on WAYR_PREWORK_CRON      (default: every 2 hours)
              run_wayr_newsletter on WAYR_NEWSLETTER_CRON (default: 06:30 UTC daily)

Schedules are fixed when you deploy, but Modal secrets are only readable inside
a running function — so the mode comes from the deploy-time env var
WAYR_PIPELINE_MODE, and is injected into every function's environment as
PIPELINE_MODE *after* the user secret, so the two can never disagree. Any
PIPELINE_MODE inside the Modal secret is therefore ignored.

Usage:
  WAYR_ENV=prod modal deploy modal_deploy.py                                  # news (default)
  WAYR_ENV=prod WAYR_PIPELINE_MODE=newsletter modal deploy modal_deploy.py    # newsletter
  WAYR_ENV=prod WAYR_PIPELINE_MODE=newsletter WAYR_NEWSLETTER_CRON="30 17 * * *" modal deploy modal_deploy.py

  modal run modal_deploy.py::run_wayr_newsletter --skip-publishing            # preview on Modal
  modal run modal_deploy.py::run_wayr_newsletter --with-prework               # one-shot on Modal
  modal run modal_deploy.py::test_connections
"""

import os
import modal
from pathlib import Path

from wayr.config import mode_schedules

# Determine environment
ENVIRONMENT = os.getenv("WAYR_ENV", "dev")  # Default to "dev"

# Deployment shape and crons — deploy-time only (see module docstring).
DEPLOY_MODE = os.getenv("WAYR_PIPELINE_MODE", "news").strip().lower()
SCHEDULES = mode_schedules(
    DEPLOY_MODE,
    pipeline_cron=os.getenv("WAYR_PIPELINE_CRON", "0 */2 * * *"),
    prework_cron=os.getenv("WAYR_PREWORK_CRON", "0 */2 * * *"),
    newsletter_cron=os.getenv("WAYR_NEWSLETTER_CRON", "30 6 * * *"),
)


# A newsletter deployment gets its own app so it never replaces (and unschedules)
# the news app for the same environment. Both share that environment's secret and
# volume — the compile job reads the pool the news pipeline fills. `both` IS the
# news app plus a newsletter, so it keeps the news app's name.
APP_NAME = f"wayr-platform-{ENVIRONMENT}" + (
    "-newsletter" if DEPLOY_MODE == "newsletter" else ""
)
app = modal.App(APP_NAME)


def _cron(key: str):
    """modal.Cron for a scheduled function, or None to deploy it unscheduled (manual only)."""
    expr = SCHEDULES[key]
    return modal.Cron(expr) if expr else None


# Create Modal app with environment suffix

# Get the project root directory
project_root = Path(__file__).parent

# Define the container image with all dependencies
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libxml2", "libxslt1.1")  # Required for newspaper3k
    .pip_install(
        "pydantic>=2.7.0",
        "pydantic-settings>=2.2.1",
        "openai>=1.30.0",
        "anthropic==0.34.0",
        "google-genai>=0.3.0",
        "feedparser>=6.0.11",
        "requests>=2.31.0",
        "requests-oauthlib>=1.3.1",  # For Twitter OAuth
        "python-dotenv>=1.0.1",
        "aiohttp>=3.9.5",
        "beautifulsoup4>=4.12.3",
        "newspaper3k>=0.2.8",
        "python-dateutil>=2.9.0",
        "tenacity>=8.2.3",
        "rich>=13.7.1",
        "lxml-html-clean>=0.4.3",
        "httpx==0.27.2",
        "supabase>=2.7.4",
        "jinja2>=3.1",
        "pyyaml>=6.0",
    )
    # Add the wayr package directory
    .add_local_dir(project_root / "wayr", remote_path="/root/wayr")
    # A newsletter's six files (sources, criteria, sections, voice, …) live under
    # newsletters/<name>/ and the secret points at them by path; the container's
    # working directory is /root, so `newsletters/<name>/sources.txt` resolves.
    .add_local_dir(project_root / "newsletters", remote_path="/root/newsletters")
)

# Configure secrets and volumes based on environment
print(f"🔧 Deploying to environment: {ENVIRONMENT}  mode: {DEPLOY_MODE}")
for _k, _v in SCHEDULES.items():
    print(f"   {_k:<10} {_v or 'unscheduled (manual only)'}")

# Every environment gets its own secret and volume, named after it. (The first
# deployment, in another Modal workspace, used `wayr-secrets` / `wayr-data-dev`
# for prod; the `wayr` workspace started clean, so the names are uniform.)
secret_name = f"wayr-secrets-{ENVIRONMENT}"
volume_name = f"wayr-data-{ENVIRONMENT}"

# Load the secret
try:
    user_secret = modal.Secret.from_name(secret_name)
except Exception:
    raise RuntimeError(
        f"Secret '{secret_name}' not found in Modal. Create it before deploying: "
        f"scripts/modal_secret.py --env-file .env.<region> --newsletter <name> --apply"
    )

# Secrets apply in order; later ones override earlier ones.
#   defaults  — /data/ paths for every state file, so the volume is used even if
#               the user secret forgot to set them (user secret still overrides)
#   user      — API keys, feeds, everything else
#   mode      — the deploy-time mode, last so it always wins
_defaults_secret = modal.Secret.from_dict(
    {
        "ARTICLE_CACHE_FILE": "/data/processed_articles.json",
        "WP_TAXONOMY_CACHE_FILE": "/data/wp_taxonomy.json",
        "FEED_LEDGER_FILE": "/data/feed_ledger.json",
        "NEWSLETTER_RUNS_FILE": "/data/newsletter_runs.json",
    }
)
_mode_secret = modal.Secret.from_dict({"PIPELINE_MODE": DEPLOY_MODE})
secrets = [_defaults_secret, user_secret, _mode_secret]

# Load the volume
print(f"💾 Using volume: {volume_name}")
volume = modal.Volume.from_name(volume_name, create_if_missing=True)


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/data": volume},  # Mount volume to persist article cache
    schedule=_cron(
        "pipeline"
    ),  # news mode: every 2 hours; newsletter mode: unscheduled
    timeout=900,  # 15 minutes max (your pipeline takes ~3 mins)
    cpu=2.0,  # 2 vCPUs
    memory=4096,  # 4GB RAM
)
async def run_wayr_pipeline():
    """
    Run the WAYR pipeline to discover, classify, generate, proofread, and publish articles.

    This function is scheduled to run every 2 hours:
    - 12am, 2am, 4am, 6am, 8am, 10am, 12pm, 2pm, 4pm, 6pm, 8pm, 10pm (UTC)
    """
    from wayr.orchestrator import WAYROrchestrator
    from wayr.utils import get_logger

    logger = get_logger(__name__)

    logger.info("=" * 60)
    logger.info("Starting WAYR pipeline on Modal...")
    logger.info("=" * 60)

    try:
        orchestrator = WAYROrchestrator()
        results = await orchestrator.run_pipeline()

        # Commit volume changes to persist the article cache
        volume.commit()
        logger.info("Article cache persisted to volume")

        logger.info("=" * 60)
        logger.info("WAYR pipeline completed successfully!")
        logger.info(f"Published: {len(results.get('published', []))} articles")
        logger.info("=" * 60)

        return {
            "status": "success",
            "discovered": len(results.get("discovered", [])),
            "classified": len(results.get("classified", [])),
            "generated": len(results.get("generated", [])),
            "edited": len(results.get("edited", [])),
            "published": len(results.get("published", [])),
        }
    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        # Still commit volume even on failure to preserve partial progress
        volume.commit()
        return {"status": "error", "error": str(e)}


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/data": volume},  # Mount volume to persist article cache
    timeout=900,
)
async def run_wayr_preview():
    """
    Run the WAYR pipeline in preview mode (no publishing).

    Use this for testing:
    modal run modal_deploy.py::run_wayr_preview
    """
    from wayr.orchestrator import WAYROrchestrator
    from wayr.utils import get_logger

    logger = get_logger(__name__)

    logger.info("Running WAYR in PREVIEW mode (no publishing)...")

    orchestrator = WAYROrchestrator()
    results = await orchestrator.preview_pipeline()

    logger.info(
        f"Preview complete - {len(results.get('edited', []))} articles generated"
    )

    return results


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/data": volume},  # Mount volume to persist article cache
    timeout=300,
)
async def test_connections():
    """
    Test all external connections (WordPress, OpenAI, Anthropic).

    Use this to verify your setup:
    modal run modal_deploy.py::test_connections
    """
    from wayr.orchestrator import WAYROrchestrator
    from wayr.utils import get_logger

    logger = get_logger(__name__)

    logger.info("Testing WAYR connections...")

    orchestrator = WAYROrchestrator()
    success = await orchestrator.test_connections()

    if success:
        logger.info("✅ All connections successful!")
    else:
        logger.error("❌ Some connections failed. Check logs above.")

    return {"success": success}


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/data": volume},
    schedule=_cron("prework"),  # newsletter mode: every 2 hours; news mode: unscheduled
    timeout=1800,  # a cold start classifies ~300 stories; steady state is a few minutes
    cpu=2.0,
    memory=4096,
)
async def run_wayr_prework():
    """
    Newsletter prework: discover → dedup → extract → classify → score into the pool.

    Never authors, never publishes. Owns feed_ledger.json and
    processed_articles.json on the volume; run_wayr_newsletter only reads them.
    """
    from wayr.orchestrator import WAYROrchestrator
    from wayr.utils import get_logger

    logger = get_logger(__name__)
    os.environ["PIPELINE_MODE"] = (
        "newsletter"  # this function is newsletter mode by construction
    )

    logger.info("=" * 60)
    logger.info("Starting WAYR prework on Modal...")
    logger.info("=" * 60)
    try:
        results = await WAYROrchestrator().run_prework()
        volume.commit()
        ledger = results.get("ledger") or {}
        logger.info(
            f"Prework complete: {len(results.get('discovered', []))} discovered, "
            f"{len(results.get('news', []))} news, {len(results.get('scored', []))} scored"
        )
        return {
            "status": "success",
            "discovered": len(results.get("discovered", [])),
            "classified": len(results.get("classified", [])),
            "news": len(results.get("news", [])),
            "scored": len(results.get("scored", [])),
            "ledger": ledger,
        }
    except Exception as e:
        logger.error(f"Prework failed: {e}", exc_info=True)
        volume.commit()
        return {"status": "error", "error": str(e)}


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/data": volume},
    schedule=_cron(
        "newsletter"
    ),  # newsletter mode: daily at :30; news mode: unscheduled
    timeout=1800,  # --with-prework on a cold pool needs the headroom
    cpu=2.0,
    memory=4096,
)
async def run_wayr_newsletter(
    skip_publishing: bool = False, with_prework: bool = False
):
    """
    Newsletter compile: window → select → summarize → compile → render → publish → email.

    Scheduled with no arguments. By hand:
      modal run modal_deploy.py::run_wayr_newsletter --skip-publishing   # preview, nothing recorded
      modal run modal_deploy.py::run_wayr_newsletter --with-prework      # one-shot
    Owns newsletter_runs.json on the volume; reads the pool, never writes it.
    The watermark advances only when the draft push succeeds.
    """
    from wayr.orchestrator import WAYROrchestrator
    from wayr.utils import get_logger

    logger = get_logger(__name__)
    os.environ["PIPELINE_MODE"] = "newsletter"

    logger.info("=" * 60)
    logger.info("Starting WAYR newsletter on Modal...")
    logger.info("=" * 60)
    try:
        orchestrator = WAYROrchestrator()
        if with_prework:
            pre = await orchestrator.run_prework()
            volume.commit()
            logger.info(f"Prework: {len(pre.get('scored', []))} stories scored")
        results = await orchestrator.run_newsletter(skip_publishing=skip_publishing)
        volume.commit()
        status = results.get("status")
        if status == "preview" and results.get("html"):
            path = f"/data/newsletter_preview_{results['window_end'][:10]}.html"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(results["html"])
            volume.commit()
            logger.info(f"Preview written to {path}")
        logger.info(f"Newsletter run: {status}")
        return {
            "status": status,
            "window": [results.get("window_start"), results.get("window_end")],
            "counts": results.get("counts"),
            "sections": results.get("sections"),
            "compile": results.get("compile"),
            "publisher": results.get("publisher"),
            "external_id": results.get("external_id"),
            "draft_url": results.get("draft_url"),
            "mail": results.get("mail"),
            "social": results.get("social"),
            "errors": results.get("errors"),
        }
    except Exception as e:
        logger.error(f"Newsletter run failed: {e}", exc_info=True)
        volume.commit()
        return {"status": "error", "error": str(e)}


# Local entrypoint for manual runs
@app.local_entrypoint()
async def main():
    """
    Local entrypoint: runs whatever this deployment's mode schedules.

      modal run modal_deploy.py                                    # news: pipeline; newsletter: one-shot
      WAYR_PIPELINE_MODE=newsletter modal run modal_deploy.py
    """
    if DEPLOY_MODE == "newsletter":
        result = await run_wayr_newsletter.remote(with_prework=True)
        print(f"\nNewsletter result: {result}")
    else:
        result = await run_wayr_pipeline.remote()
        print(f"\nPipeline result: {result}")
