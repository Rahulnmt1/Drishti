#!/usr/bin/env bash
# LaunchAgent wrapper for the daily refresh, with first-success-wins logic.
#
# Schedule (set by install.sh): fires hourly between 10:00 and 15:00 local
# time, every day. The first attempt that succeeds writes a per-day flag
# file; subsequent attempts the same day detect the flag and exit 0
# without doing any work. So in steady state, only ONE engine run happens
# per calendar day, and the other 5 firings cost almost nothing.
#
# Exit codes:
#   0 = engine exit 0, OR today already succeeded (flag present, no-op)
#   1 = engine returned non-zero  (caller may retry — next hour's launchd
#       fire is the actual retry, no in-process loop here)
#   2 = pre-flight failed (paths, python missing, etc.)
#
# Files this script reads/writes (all under _engine/_logs/):
#   wrapper_YYYY-MM-DD_HHMM.log   per-firing log (always created)
#   .success_YYYY-MM-DD.flag      created on first success of the day
#   scheduler_status.txt          rolling 7-day x 6-hour table (regenerated every fire)
#
# Resolved by install.sh (and overridable for manual testing):
#   KB_ROOT     – absolute path to the KnowledgeBase folder
#   PYTHON_BIN  – absolute path to a python3 with the engine's deps installed
#
# Manual invocation examples:
#   ./run_daily.sh                                  # honors flag (skip if today already done)
#   FORCE=1 ./run_daily.sh                          # ignores flag, always runs
#   KB_ROOT=/path PYTHON_BIN=/opt/.../python3 ./run_daily.sh

set -u
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # _engine/_scheduler/
# KB_ROOT defaults to two levels up (KnowledgeBase/), overridable via env.
KB_ROOT="${KB_ROOT:-$(cd "$HERE/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/env python3}"
FORCE="${FORCE:-0}"

LOG_DIR="$KB_ROOT/_engine/_logs"
mkdir -p "$LOG_DIR"

TODAY="$(date +%Y-%m-%d)"
STAMP="$(date +%Y-%m-%d_%H%M)"
SUCCESS_FLAG="$LOG_DIR/.success_${TODAY}.flag"
WRAPPER_LOG="$LOG_DIR/wrapper_${STAMP}.log"

# Run the wrapper body in a function so we can capture its exit code WITHOUT
# `exit` short-circuiting the cleanup + status-render steps that follow.
# (The previous version had every code path call `exit` from inside a
# `{ ... } >> log 2>&1` group, which terminated the script before cleanup ran.)
_wrapper_body() {
  echo "=== banking-kb daily refresh wrapper ==="
  echo "started_at:    $(date -Iseconds)"
  echo "host:          $(hostname)"
  echo "kb_root:       $KB_ROOT"
  echo "python_bin:    $PYTHON_BIN"
  echo "engine:        $KB_ROOT/_engine/run.py"
  echo "success_flag:  $SUCCESS_FLAG"
  echo "force_mode:    $FORCE"
  echo ""

  # First-success-wins: skip if today's run already succeeded.
  if [[ "$FORCE" != "1" && -f "$SUCCESS_FLAG" ]]; then
    echo "SKIP: today already succeeded — no work to do."
    echo "      flag contents:"
    sed 's/^/        /' "$SUCCESS_FLAG"
    echo ""
    echo "      To force a re-run within the day:"
    echo "        rm '$SUCCESS_FLAG' && '$0'"
    echo "        # or:  FORCE=1 '$0'"
    echo "finished_at:   $(date -Iseconds)"
    return 0
  fi

  if [[ ! -f "$KB_ROOT/_engine/run.py" ]]; then
    echo "FATAL: engine not found at $KB_ROOT/_engine/run.py" >&2
    return 2
  fi
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 && [[ ! -x "$PYTHON_BIN" ]]; then
    echo "FATAL: PYTHON_BIN '$PYTHON_BIN' not executable" >&2
    return 2
  fi

  cd "$KB_ROOT"
  "$PYTHON_BIN" "$KB_ROOT/_engine/run.py" --mode daily
  local engine_rc=$?

  echo ""
  echo "engine_exit:   $engine_rc"
  echo "finished_at:   $(date -Iseconds)"

  if [[ "$engine_rc" -eq 0 ]]; then
    {
      echo "completed_at=$(date -Iseconds)"
      echo "wrapper_log=$WRAPPER_LOG"
      echo "engine_exit=0"
      # Best-effort: link to the engine's own JSON run summary if findable.
      local latest_json
      latest_json=$(ls -1t "$LOG_DIR"/run_*.json 2>/dev/null | head -1 || true)
      if [[ -n "$latest_json" ]]; then
        echo "engine_run_log=$latest_json"
      fi
    } > "$SUCCESS_FLAG"
    echo ""
    echo "OK: wrote $SUCCESS_FLAG (remaining hourly fires today will skip)."
  else
    echo ""
    echo "FAIL: engine exit=$engine_rc. The next hourly launchd fire will retry."
    echo "      (Retry window is 10:00–15:00 local time. After 15:00, the"
    echo "       day is over and the next attempt is tomorrow at 10:00.)"
  fi

  return "$engine_rc"
}

_wrapper_body >>"$WRAPPER_LOG" 2>&1
RC=$?

# Cleanup runs on EVERY fire — including SKIP and FAIL — because hygiene
# shouldn't depend on engine success.
#
# Retention policy:
#   * wrapper_*.log     — keep last 7 days (matches the scheduler_status.txt window)
#   * .success_*.flag   — keep last 14 days (one full pay period for audit)
find "$LOG_DIR" -maxdepth 1 -name 'wrapper_*.log'    -type f -mtime +7  -delete 2>/dev/null || true
find "$LOG_DIR" -maxdepth 1 -name '.success_*.flag' -type f -mtime +14 -delete 2>/dev/null || true

# Regenerate the rolling 7-day status table. Always runs (SKIP/OK/FAIL) so
# the file stays current after every fire. Best-effort: render errors must
# not poison the wrapper's exit code, which launchd uses for retry decisions.
if [[ -x "$HERE/render_status.py" ]]; then
  "$PYTHON_BIN" "$HERE/render_status.py" "$LOG_DIR" >/dev/null 2>&1 || true
fi

exit "$RC"
