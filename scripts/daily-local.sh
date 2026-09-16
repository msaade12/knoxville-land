#!/bin/zsh
# Daily sweep from the owner's Mac. Zillow blocks cloud servers, so this is the
# run that actually refreshes Zillow; the GitHub Action is the Redfin-only fallback.
# Installed by scripts/install-launchd.sh; logs to logs/daily-local.log.
set -eu
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."
mkdir -p logs
{
  echo "=== $(date '+%Y-%m-%d %H:%M') ==="
  git pull -q --rebase --autostash origin main || { echo "pull failed"; exit 1; }
  python3 scripts/sweep.py
  git add data/tracts.json data/report.json data/excluded.json photos
  if git diff --cached --quiet; then echo "nothing changed"; exit 0; fi
  git -c user.name="land-scout (mac)" -c user.email="msaade@global.rutgers.edu" \
      commit -q -m "$(python3 scripts/report.py --commit-message) [mac]"
  git pull -q --rebase --autostash origin main && git push -q origin main
  echo "pushed"
} >> logs/daily-local.log 2>&1
