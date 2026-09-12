#!/usr/bin/env bash
# Refresh the exported data and deploy to Vercel. The macOS/Linux twin of
# deploy.ps1 -- same two steps, same two things deployed: the static page in web/
# and the live endpoint api/screener.py.
#
#   ./deploy.sh              # preview deployment
#   ./deploy.sh --prod       # production URL
#
# `vercel login` authenticates as you, in a browser, and cannot be automated. If
# you are not logged in this stops and says so rather than failing halfway.
set -euo pipefail
cd "$(dirname "$0")"

DB="${SCREENER_DB_PATH:-data/demo.duckdb}"
PYTHON="${PYTHON:-python3}"

if ! command -v vercel >/dev/null 2>&1; then
  echo "Vercel CLI not found. Install it with: npm install -g vercel" >&2
  exit 1
fi

if [ -f "$DB" ]; then
  echo "== Refreshing web/screener-data.json from $DB =="
  "$PYTHON" -m export_web --db "$DB" --out web/screener-data.json
else
  # Not an error. The live endpoint does not read this file, so a deploy with no
  # collected database still produces a working page.
  echo "== No database at $DB; deploying the existing export =="
fi

echo
echo "== Checking Vercel auth =="
if ! vercel whoami >/dev/null 2>&1; then
  echo "Not logged in to Vercel. Run this once, then re-run:" >&2
  echo "  vercel login" >&2
  exit 1
fi

echo
echo "== Deploying web/ + api/ =="
if [ "${1:-}" = "--prod" ] || [ "${1:-}" = "-Production" ]; then
  vercel deploy --prod --yes
else
  vercel deploy --yes
fi
