#!/bin/zsh
# Installs a launchd job that runs scripts/daily-local.sh at 7:00 every morning.
# If the Mac is asleep at 7:00 the job runs when it next wakes.
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.knoxville-land.daily.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.knoxville-land.daily</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>$REPO/scripts/daily-local.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$REPO/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$REPO/logs/launchd.err</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed: $PLIST"
launchctl list | grep knoxville-land || true
