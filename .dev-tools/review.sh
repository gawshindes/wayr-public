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
