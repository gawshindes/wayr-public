#!/usr/bin/env bash
# ============================================================
# new-feature.sh — Start a new feature branch with guardrails
#
# Usage:
#   ./.dev-tools/new-feature.sh "add retry logic to web scraper"
# ============================================================

set -euo pipefail

DESCRIPTION="${1:?Usage: ./.dev-tools/new-feature.sh \"description of the feature\"}"

# Sanitize description into branch name
BRANCH_NAME="feat/$(echo "$DESCRIPTION" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-' | head -c 50)"

# Make sure we're starting from latest main
echo "🔄 Updating main..."
git checkout main
git pull origin main 2>/dev/null || true

# Create feature branch
echo "🌿 Creating branch: $BRANCH_NAME"
git checkout -b "$BRANCH_NAME"

echo ""
echo "✅ Ready to develop!"
echo ""
echo "When done, run: ./.dev-tools/review.sh"
echo "Then merge:     git checkout main && git merge $BRANCH_NAME"
