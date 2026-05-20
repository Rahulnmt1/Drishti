#!/usr/bin/env bash
# Wrapper that launchd invokes once a day. Runs the engine and writes a
# rotating wrapper-log under _engine/_logs/ so failures are debuggable even
# when the engine itself didn't get far enough to write its JSON run log.
#
# Exit codes:
#   0  = engine exit 0
#   1  = engine returned non-zero
#   2  = pre-flight failed (paths, python missing, etc.)
#
# Resolved by install.sh:
#   KB_ROOT     – absolute path to the KnowledgeBase folder
#   PYTHON_BIN  – absolute path to a python3 with the engine's deps installed
#
# When run manually you can override either via env, e.g.:
#   KB_ROOT=/path/to/KnowledgeBase PYTHON_BIN=/opt/homebrew/bin/python3 ./run_daily.sh

set -u
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # _engine/_scheduler/
# KB_ROOT defaults to two levels up (KnowledgeBase/), overridable via env.
KB_ROOT="${KB_ROOT:-$(cd "$HERE/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/env python3}"

LOG_DIR="$KB_ROOT/_engine/_logs"
mkdir -p "$LOG_DIR"

STAMP="$(date +%Y-%m-%d_%H%M)"
WRAPPER_LOG="$LOG_DIR/wrapper_${STAMP}.log"

{
  echo "=== banking-kb daily refresh wrapper ==="
  echo "started_at:   $(date -Iseconds)"
  echo "host:         $(hostname)"
  echo "kb_root:      $KB_ROOT"
  echo "python_bin:   $PYTHON_BIN"
  echo "engine:       $KB_ROOT/_engine/run.py"
  echo ""

  if [[ ! -f "$KB_ROOT/_engine/run.py" ]]; then
    echo "FATAL: engine not found at $KB_ROOT/_engine/run.py" >&2
    exit 2
  fi
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 && [[ ! -x "$PYTHON_BIN" ]]; then
    echo "FATAL: PYTHON_BIN '$PYTHON_BIN' not executable" >&2
    exit 2
  fi

  cd "$KB_ROOT"
  "$PYTHON_BIN" "$KB_ROOT/_engine/run.py" --mode daily
  ENGINE_RC=$?

  echo ""
  echo "engine_exit:  $ENGINE_RC"
  echo "finished_at:  $(date -Iseconds)"
  exit "$ENGINE_RC"
} >>"$WRAPPER_LOG" 2>&1

# Keep only the most recent 30 wrapper logs (engine JSON logs are kept forever).
ls -1t "$LOG_DIR"/wrapper_*.log 2>/dev/null | tail -n +31 | xargs -I{} rm -f "{}" || true
