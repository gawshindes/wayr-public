#!/usr/bin/env bash
# ============================================================
# setup-hooks.sh — Install git hooks for solo dev workflow
# Run once: chmod +x setup-hooks.sh && ./setup-hooks.sh
# ============================================================

set -euo pipefail

HOOKS_DIR=".git/hooks"
SCRIPTS_DIR=".dev-tools"

echo "🔧 Setting up git hooks for agentic app development..."

# --- Detect project type ---
if [ -f "package.json" ]; then
  PROJECT_TYPE="node"
  echo "  Detected: Node.js project"
elif [ -f "pyproject.toml" ] || [ -f "requirements.txt" ] || [ -f "setup.py" ]; then
  PROJECT_TYPE="python"
  echo "  Detected: Python project"
else
  PROJECT_TYPE="unknown"
  echo "  ⚠️  Could not detect project type. Will create generic hooks."
fi

mkdir -p "$HOOKS_DIR"
mkdir -p "$SCRIPTS_DIR"

# ============================================================
# 1. PRE-COMMIT HOOK — lint + type check + format
# ============================================================
cat > "$HOOKS_DIR/pre-commit" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail

echo "🔍 Pre-commit: Running checks..."

# --- Node.js ---
if [ -f "package.json" ]; then
  # Lint
  if npx --no-install eslint --version &>/dev/null; then
    echo "  → ESLint..."
    npx eslint . --ext .ts,.tsx,.js,.jsx --max-warnings 0
  fi

  # Type check
  if npx --no-install tsc --version &>/dev/null; then
    echo "  → TypeScript..."
    npx tsc --noEmit
  fi

  # Format check
  if npx --no-install prettier --version &>/dev/null; then
    echo "  → Prettier..."
    npx prettier --check .
  fi
fi

# --- Python ---
if [ -f "pyproject.toml" ] || [ -f "requirements.txt" ]; then
  if command -v poetry &>/dev/null; then
    # Lint
    echo "  → Ruff..."
    poetry run ruff check wayr/

    # Type check (non-blocking — warns only, does not fail commit)
    echo "  → Mypy (warnings only)..."
    poetry run mypy wayr/ --ignore-missing-imports || echo "  ⚠️  Mypy found type issues (non-blocking)"

    # Format check
    echo "  → Black..."
    poetry run black --check wayr/
  else
    # Fallback to global installs
    if command -v ruff &>/dev/null; then
      echo "  → Ruff..."
      ruff check wayr/
    fi
    if command -v mypy &>/dev/null; then
      echo "  → Mypy (warnings only)..."
      mypy wayr/ --ignore-missing-imports || echo "  ⚠️  Mypy found type issues (non-blocking)"
    fi
    if command -v black &>/dev/null; then
      echo "  → Black..."
      black --check wayr/
    fi
  fi
fi

# --- Agentic app safety scan (universal) ---
echo "  → Scanning for agentic antipatterns..."

# Check for unbounded loops (while(true) without maxIterations nearby)
RISKY_LOOPS=$(grep -rn "while\s*(true)\|while\s*True\|for\s*(\s*;\s*;\s*)" --include="*.ts" --include="*.tsx" --include="*.py" --include="*.js" wayr/ 2>/dev/null || true)
if [ -n "$RISKY_LOOPS" ]; then
  echo ""
  echo "  ⚠️  Potentially unbounded loops found:"
  echo "$RISKY_LOOPS"
  echo ""
  echo "  Make sure each has a maxIterations guard."
  echo "  Add '// bounded-loop-ok' comment to suppress this warning."
  echo ""
  # Filter out acknowledged ones
  UNACKED=$(echo "$RISKY_LOOPS" | grep -v "bounded-loop-ok" || true)
  if [ -n "$UNACKED" ]; then
    echo "  ❌ Unacknowledged unbounded loops. Fix or add '// bounded-loop-ok'."
    exit 1
  fi
fi

# Check for raw user input in system prompts
RAW_INPUT=$(grep -rn "system.*\${.*input\|system.*\${.*query\|system.*\${.*message\|system.*f\".*{.*input\|system.*f\".*{.*query" --include="*.ts" --include="*.py" --include="*.js" src/ 2>/dev/null || true)
if [ -n "$RAW_INPUT" ]; then
  echo ""
  echo "  ❌ Possible prompt injection risk — raw user input in system prompt:"
  echo "$RAW_INPUT"
  exit 1
fi

echo "✅ Pre-commit passed!"
HOOK
chmod +x "$HOOKS_DIR/pre-commit"
echo "  ✅ Installed pre-commit hook"

# ============================================================
# 2. PRE-PUSH HOOK — run full test suite
# ============================================================
cat > "$HOOKS_DIR/pre-push" << 'HOOK'
#!/usr/bin/env bash
set -euo pipefail

echo "🧪 Pre-push: Running tests..."

# --- Node.js ---
if [ -f "package.json" ]; then
  if grep -q '"test"' package.json; then
    npm test
  else
    echo "  ⚠️  No test script in package.json. Add one!"
  fi
fi

# --- Python ---
if [ -f "pyproject.toml" ] || [ -f "requirements.txt" ]; then
  if command -v poetry &>/dev/null; then
    echo "  → pytest (via poetry)..."
    poetry run pytest tests/ -x -q
  elif command -v pytest &>/dev/null; then
    pytest tests/ -x -q
  else
    echo "  ⚠️  pytest not found. Install it: poetry install"
  fi
fi

echo "✅ Pre-push passed!"
HOOK
chmod +x "$HOOKS_DIR/pre-push"
echo "  ✅ Installed pre-push hook"

# ============================================================
# 3. REVIEW SCRIPT — trigger Claude Code review on a branch
# ============================================================
cat > "$SCRIPTS_DIR/review.sh" << 'SCRIPT'
#!/usr/bin/env bash
# ============================================================
# review.sh — Automated code review using Claude Code
#
# Usage:
#   ./.dev-tools/review.sh              # Review current branch vs main
#   ./.dev-tools/review.sh feat/auth    # Review specific branch vs main
#   ./.dev-tools/review.sh --full       # Full codebase review (not just diff)
# ============================================================

set -euo pipefail

BRANCH="${1:-$(git branch --show-current)}"
BASE="main"
MODE="diff"

if [ "$BRANCH" = "--full" ]; then
  MODE="full"
fi

echo "🔍 Code Review — Mode: $MODE"
echo "================================================"

if [ "$MODE" = "diff" ]; then
  # Get the diff
  DIFF=$(git diff "$BASE"..."$BRANCH" 2>/dev/null || git diff HEAD~5)

  if [ -z "$DIFF" ]; then
    echo "No changes to review."
    exit 0
  fi

  DIFF_STATS=$(git diff --stat "$BASE"..."$BRANCH" 2>/dev/null || git diff --stat HEAD~5)

  echo "Changes to review:"
  echo "$DIFF_STATS"
  echo ""
  echo "Sending to Claude Code for review..."
  echo ""

  # Write diff to temp file for Claude Code
  DIFF_FILE=$(mktemp /tmp/review-diff-XXXXXX.patch)
  echo "$DIFF" > "$DIFF_FILE"

  # Launch Claude Code with review prompt
  # The user runs this and Claude Code picks it up
  cat << EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 REVIEW PROMPT — Copy this into Claude Code:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Review the git diff in $DIFF_FILE against these criteria:

## Security
- [ ] No prompt injection risks (raw user input in system prompts)
- [ ] No exposed secrets or API keys
- [ ] Input validation on all external inputs

## Agentic Reliability
- [ ] All loops have maxIterations guards
- [ ] Token/cost budgets are enforced
- [ ] All external calls have timeouts
- [ ] Retry logic has backoff and max retries
- [ ] Graceful degradation on tool failures

## Observability
- [ ] Agent decisions are logged with reasoning
- [ ] Token usage and costs are tracked
- [ ] Errors include context (tool name, input summary)

## Code Quality
- [ ] Functions are <40 lines
- [ ] All types are explicit (no 'any')
- [ ] Error handling is meaningful (no silent catches)
- [ ] Tests exist for happy path and failure path

Flag issues as: 🔴 CRITICAL | 🟡 WARNING | 🔵 SUGGESTION

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF

else
  # Full codebase review
  cat << EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 FULL REVIEW PROMPT — Copy this into Claude Code:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Do a full architecture review of this codebase. Focus on:

1. **Agent safety audit**: Find any agent loops without iteration limits, missing timeouts, or missing budget caps. List every instance.

2. **Prompt injection surface**: Identify every place where user input flows into LLM prompts. Are they all sanitized?

3. **Error handling audit**: Find all external calls (LLM, API, tool). Do they all have try/catch, timeouts, and meaningful error messages?

4. **Cost runaway risk**: Could any code path make unbounded LLM calls? Is there a global cost tracking mechanism?

5. **Test coverage gaps**: Which agent workflows lack tests? Which failure paths are untested?

Produce a report with:
- 🔴 CRITICAL issues (fix before deploying)
- 🟡 WARNINGS (fix soon)
- 🔵 SUGGESTIONS (nice to have)
- A prioritized action plan

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
fi
SCRIPT
chmod +x "$SCRIPTS_DIR/review.sh"
echo "  ✅ Created review script at .dev-tools/review.sh"

# ============================================================
# 4. BRANCH HELPER — automates step 1 (branch-based dev)
# ============================================================
cat > "$SCRIPTS_DIR/new-feature.sh" << 'SCRIPT'
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
SCRIPT
chmod +x "$SCRIPTS_DIR/new-feature.sh"
echo "  ✅ Created branch helper at .dev-tools/new-feature.sh"

# ============================================================
echo ""
echo "🎉 Setup complete! Here's your workflow:"
echo ""
echo "  1. Start work:    ./.dev-tools/new-feature.sh \"your feature description\""
echo "  2. Code with Claude Code (CLAUDE.md rules are auto-enforced)"
echo "  3. Commit:        git hooks run lint + type check + safety scan automatically"
echo "  4. Review:        ./.dev-tools/review.sh"
echo "  5. Push:          git hooks run tests automatically"
echo "  6. Merge to main"
echo ""
