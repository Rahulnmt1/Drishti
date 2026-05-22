#!/usr/bin/env bash
# Snapshot of the daily scheduler's state:
#   - Is the LaunchAgent loaded?
#   - Did today's run already succeed (flag present)?
#   - When is the next hourly fire?
#   - What was the freshest engine + wrapper log?
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # _engine/_scheduler/
KB_ROOT="$(cd "$HERE/../.." && pwd)"                    # KnowledgeBase/
LOG_DIR="$KB_ROOT/_engine/_logs"
LABEL="com.rahul.banking-kb-daily-refresh"

TODAY="$(date +%Y-%m-%d)"
SUCCESS_FLAG="$LOG_DIR/.success_${TODAY}.flag"
NOW_HOUR=$(date +%-H)

echo "=== launchd ==="
if launchctl list | grep -q "$LABEL"; then
  launchctl list | grep "$LABEL" | awk '{printf "  loaded: yes  pid=%s  last_exit=%s\n", $1, $2}'
  echo "  schedule: hourly 10:00-15:00 local, every day"

  # Next fire computation: which of {10, 11, 12, 13, 14, 15} is next from now?
  NEXT_HOUR=""
  for h in 10 11 12 13 14 15; do
    if (( h > NOW_HOUR )); then NEXT_HOUR="$h"; break; fi
  done
  if [[ -z "$NEXT_HOUR" ]]; then
    echo "  next fire: tomorrow 10:00 (today's 10:00-15:00 window has passed)"
  else
    printf "  next fire: today %02d:00\n" "$NEXT_HOUR"
  fi
else
  echo "  loaded: no   (run ./install.sh)"
fi

echo
echo "=== today's success flag ==="
if [[ -f "$SUCCESS_FLAG" ]]; then
  echo "  PRESENT — today already succeeded, remaining hourly fires will skip."
  sed 's/^/    /' "$SUCCESS_FLAG"
  echo
  echo "  To force a re-run within the day:"
  echo "    rm '$SUCCESS_FLAG' && '$HERE/run_daily.sh'"
  echo "    # or:  FORCE=1 '$HERE/run_daily.sh'"
else
  echo "  absent — engine has not succeeded yet today."
  echo "  flag path: $SUCCESS_FLAG"
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
