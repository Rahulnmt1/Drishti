#!/usr/bin/env bash
# Shows whether the LaunchAgent is loaded, when it last ran, and the freshest log.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # _engine/_scheduler/
KB_ROOT="$(cd "$HERE/../.." && pwd)"                    # KnowledgeBase/
LOG_DIR="$KB_ROOT/_engine/_logs"
LABEL="com.rahul.banking-kb-daily-refresh"

echo "=== launchd ==="
if launchctl list | grep -q "$LABEL"; then
  launchctl list | grep "$LABEL" | awk '{printf "  loaded: yes  pid=%s  last_exit=%s\n", $1, $2}'
else
  echo "  loaded: no   (run ./install.sh)"
fi

echo
echo "=== latest engine log ==="
LATEST_JSON=$(ls -1t "$LOG_DIR"/run_*.json 2>/dev/null | head -1 || true)
if [[ -n "$LATEST_JSON" ]]; then
  ls -l "$LATEST_JSON"
else
  echo "  (none yet)"
fi

echo
echo "=== latest wrapper log ==="
LATEST_WRAP=$(ls -1t "$LOG_DIR"/wrapper_*.log 2>/dev/null | head -1 || true)
if [[ -n "$LATEST_WRAP" ]]; then
  ls -l "$LATEST_WRAP"
  echo "--- tail ---"
  tail -20 "$LATEST_WRAP"
else
  echo "  (none yet — agent has not fired)"
fi
