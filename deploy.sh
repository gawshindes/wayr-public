#!/bin/bash
# WAYR Deployment Helper Script
# Makes it easy to deploy to different environments

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 WAYR Deployment Helper${NC}"
echo ""

# Check if environment is specified
if [ -z "$1" ]; then
    echo "Usage: ./deploy.sh [prod|india] [action]"
    echo ""
    echo "Environments:"
    echo "  prod     - US production (wayr.today)"
    echo "  india    - India region"
    echo ""
    echo "Actions:"
    echo "  release  - deploy + upload the secret + connection test, in that order (the way production changes)"
    echo "  deploy   - Deploy to Modal (scheduled, runs every 2 hours)"
    echo "  run      - Trigger one-time run now"
    echo "  preview  - Test locally (no publish)"
    echo "  test     - Test connections"
    echo "  logs     - View logs"
    echo "  stop     - Stop scheduled runs"
    echo ""
    echo "Mode (env var, default news):"
    echo "  WAYR_PIPELINE_MODE=newsletter ./deploy.sh <env> deploy   # prework + daily compile, no per-story pipeline"
    echo "  WAYR_PIPELINE_MODE=both ./deploy.sh <env> deploy         # news site + daily newsletter from the same research"
    echo "  WAYR_NEWSLETTER_CRON='30 17 * * *' to move the daily compile (keep :30)"
    echo ""
    echo "Examples:"
    echo "  WAYR_PIPELINE_MODE=newsletter WAYR_NEWSLETTER_CRON='30 12 * * *' ./deploy.sh prod release   # wayrtoday: code + secret + test"
    echo "  ./deploy.sh prod deploy   # Deploy to US production"
    echo "  ./deploy.sh india deploy  # Deploy to India"
    echo "  ./deploy.sh prod run      # Trigger US run now"
    echo "  ./deploy.sh prod logs     # View US logs"
    echo ""
    exit 1
fi

ENV=$1
ACTION=${2:-deploy}
MODE=${WAYR_PIPELINE_MODE:-news}   # news | newsletter — decides which functions get a schedule

echo -e "${BLUE}Environment:${NC} $ENV"
echo -e "${BLUE}Mode:${NC}        $MODE"
echo -e "${BLUE}Action:${NC}      $ACTION"
echo ""

case $ACTION in
    deploy)
        # Calculate Configuration for Display
        APP_NAME="wayr-platform-$ENV"
        [[ "$MODE" == "newsletter" ]] && APP_NAME="wayr-platform-$ENV-newsletter"   # never replaces the news app
        SECRET_NAME="wayr-secrets-$ENV"
        VOLUME_NAME="wayr-data-$ENV"
        WORKSPACE=$(poetry run modal profile current 2>/dev/null || echo "?")

        echo -e "${YELLOW}----------------------------------------${NC}"
        echo -e "${YELLOW}🚀 DEPLOYMENT PREVIEW${NC}"
        echo -e "${YELLOW}----------------------------------------${NC}"
        echo -e "Environment : ${BLUE}$ENV${NC}"
        echo -e "Workspace   : ${BLUE}$WORKSPACE${NC} (Modal profile)"
        echo -e "App Name    : ${BLUE}$APP_NAME${NC}"
        echo -e "Mode        : ${BLUE}$MODE${NC}"
        if [[ "$MODE" == "newsletter" ]]; then
            echo -e "Schedules   : ${BLUE}prework ${WAYR_PREWORK_CRON:-0 */2 * * *} · newsletter ${WAYR_NEWSLETTER_CRON:-30 6 * * *}${NC}"
        elif [[ "$MODE" == "both" ]]; then
            echo -e "Schedules   : ${BLUE}pipeline ${WAYR_PIPELINE_CRON:-0 */2 * * *} (also scores the pool) · newsletter ${WAYR_NEWSLETTER_CRON:-30 6 * * *}${NC}"
        else
            echo -e "Schedules   : ${BLUE}pipeline ${WAYR_PIPELINE_CRON:-0 */2 * * *}${NC}"
        fi
        echo -e "Secret      : ${BLUE}$SECRET_NAME${NC}"
        echo -e "Volume      : ${BLUE}$VOLUME_NAME${NC}"
        echo -e "${YELLOW}----------------------------------------${NC}"
        echo ""

        read -p "Are you sure you want to deploy? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo -e "${GREEN}Deploying to $ENV...${NC}"
            WAYR_ENV=$ENV WAYR_PIPELINE_MODE=$MODE poetry run modal deploy modal_deploy.py
            echo ""
            echo -e "${GREEN}✅ Deployed to $ENV!${NC}"
            echo "View logs: ./deploy.sh $ENV logs"
        else
            echo -e "${RED}Deployment cancelled.${NC}"
            exit 0
        fi
        ;;

    release)
        # The one way production should change: code and secret move together, in the
        # order that is always safe (new code reads old and new setting names; the new
        # secret may use names only the new code knows), then a connection test inside
        # Modal so a bad release fails here, not at the next scheduled run.
        NEWSLETTER=${WAYR_NEWSLETTER:-wayrtoday}
        ENV_FILE=${WAYR_ENV_FILE:-.env.$([[ "$ENV" == "prod" ]] && echo usa || echo "$ENV")}
        [[ -f "$ENV_FILE" ]] || { echo -e "${RED}no env file $ENV_FILE (set WAYR_ENV_FILE)${NC}"; exit 1; }
        [[ -d "newsletters/$NEWSLETTER" ]] || { echo -e "${RED}no newsletters/$NEWSLETTER (set WAYR_NEWSLETTER)${NC}"; exit 1; }
        echo -e "${YELLOW}Release: $(git rev-parse --short HEAD)$(git diff --quiet || echo ' + uncommitted changes') → $ENV ($MODE), secret from $ENV_FILE + newsletters/$NEWSLETTER${NC}"
        echo -e "${GREEN}[1/3] deploy${NC}"
        WAYR_ENV=$ENV WAYR_PIPELINE_MODE=$MODE poetry run modal deploy modal_deploy.py
        echo -e "${GREEN}[2/3] secret${NC}"
        poetry run python scripts/modal_secret.py --env-file "$ENV_FILE" --newsletter "$NEWSLETTER" --env "$ENV" --apply | grep -vE "^\s+os\.getenv|^Use it in your Modal app|^@app|^def some_function|^\s*$"
        echo -e "${GREEN}[3/3] connection test inside Modal${NC}"
        if WAYR_ENV=$ENV WAYR_PIPELINE_MODE=$MODE poetry run modal run modal_deploy.py::test_connections 2>&1 | tee /dev/stderr | grep -q "All connections successful"; then
            echo -e "${GREEN}✅ Released $(git rev-parse --short HEAD) to $ENV${NC}"
        else
            echo -e "${RED}❌ Connection test failed after release — the schedule will fail the same way; fix and release again${NC}"
            exit 1
        fi
        ;;

    run)
        echo -e "${GREEN}Triggering manual run on $ENV...${NC}"
        WAYR_ENV=$ENV WAYR_PIPELINE_MODE=$MODE poetry run modal run modal_deploy.py
        ;;

    preview)
        if [[ "$MODE" == "newsletter" ]]; then
            echo -e "${YELLOW}Running newsletter preview locally (uses .env file)...${NC}"
            poetry run wayr newsletter --with-prework --skip-publishing
        else
            echo -e "${YELLOW}Running preview locally (uses .env file)...${NC}"
            poetry run wayr preview
        fi
        ;;

    test)
        echo -e "${YELLOW}Testing connections on $ENV...${NC}"
        WAYR_ENV=$ENV WAYR_PIPELINE_MODE=$MODE poetry run modal run modal_deploy.py::test_connections
        ;;

    logs)
        echo -e "${GREEN}Viewing logs for $ENV...${NC}"
        APP_NAME="wayr-platform-$ENV"; [[ "$MODE" == "newsletter" ]] && APP_NAME="$APP_NAME-newsletter"
        poetry run modal app logs $APP_NAME
        ;;

    stop)
        echo -e "${RED}Stopping scheduled runs for $ENV...${NC}"
        read -p "Are you sure? (y/N) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            APP_NAME="wayr-platform-$ENV"; [[ "$MODE" == "newsletter" ]] && APP_NAME="$APP_NAME-newsletter"
            poetry run modal app stop $APP_NAME
            echo -e "${GREEN}✅ Stopped $APP_NAME${NC}"
        else
            echo "Cancelled"
        fi
        ;;

    *)
        echo -e "${RED}Unknown action: $ACTION${NC}"
        echo "Valid actions: release, deploy, run, preview, test, logs, stop"
        exit 1
        ;;
esac
