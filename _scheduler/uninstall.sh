#!/usr/bin/env bash
# Removes the banking-kb daily refresh LaunchAgent.
set -euo pipefail

LABEL="com.rahul.banking-kb-daily-refresh"
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [[ -f "$TARGET" ]]; then
  launchctl unload "$TARGET" 2>/dev/null || true
  rm -f "$TARGET"
  echo "Removed $TARGET"
else
  echo "Nothing to remove — $TARGET does not exist."
fi
