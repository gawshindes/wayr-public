#!/usr/bin/env bash
# demo.sh — run one demo newsletter, one step at a time.
#
#   ./demo.sh <name> <step> [args]
#
# name:  a folder under newsletters/ (ai-daily, tech-links, austin-local, nyc-local, own-site, wayr-daily)
# step:  env      assemble .env.demo-<name> and show the resolved config
#        check    which feeds actually deliver stories (run on any new or changed sources file)
#        prework  discover → classify → score into the pool          (not for wayr-daily)
#        import   seed the pool from the news pipeline's Supabase     (wayr-daily only; [days])
#        plan     what the next issue would take — nothing runs, nothing recorded
#        dump     scored candidate CSV — calibrate criteria and thresholds from it
#        preview  compile to HTML under data/<name>/ — nothing pushed, nothing recorded
#        draft    the real thing: compile, push a draft, record the run
#        day      prework (or import) then draft — a whole day in one command
#        reset    delete the demo's local state for a cold start          (never for wayr-daily)
#
# Secrets live in ONE gitignored file, .env.demo.base (API keys, staging WordPress
# credentials, Supabase). Each demo's newsletters/<name>/env.overrides is appended to it.
set -euo pipefail
name=${1:-}; step=${2:-}
usage() { sed -n '2,20p' "$0"; exit 1; }
[ -n "$name" ] && [ -n "$step" ] || usage
dir="newsletters/$name"; env=".env.demo-$name"
[ -d "$dir" ] || { echo "no such demo: $dir"; exit 1; }
[ -f .env.demo.base ] || { echo ".env.demo.base is missing — copy your secrets there (see newsletters/README.md)"; exit 1; }
assemble() { { cat .env.demo.base; echo; cat "$dir/env.overrides"; } > "$env"; }
run() { poetry run wayr "$@" --env-file "$env"; }
assemble
case "$step" in
  env)      run config ;;
  check)    run check-sources ;;
  prework)  [ "$name" != wayr-daily ] || { echo "wayr-daily takes 'import', not prework"; exit 1; }; run prework ;;
  import)   [ "$name" = wayr-daily ] || { echo "only wayr-daily imports a pool"; exit 1; }; run import-pool --days "${3:-7}" ;;
  plan)     run newsletter --plan ;;
  dump)     run newsletter --dump-candidates --skip-publishing ;;
  preview)  run newsletter --skip-publishing ;;
  draft)    run newsletter ;;
  day)      if [ "$name" = wayr-daily ]; then run import-pool --days "${3:-2}"; else run prework; fi; run newsletter ;;
  reset)    [ "$name" != wayr-daily ] || { echo "never reset wayr-daily — the pool is the point"; exit 1; }; run reset --newsletter --yes ;;
  *)        usage ;;
esac
