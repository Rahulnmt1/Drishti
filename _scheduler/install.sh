#!/usr/bin/env bash
# Installs the banking-kb daily refresh as a macOS LaunchAgent.
#
# Schedule (fixed): every day, fires hourly at 10:00, 11:00, 12:00, 13:00,
# 14:00 and 15:00 local time. The wrapper run_daily.sh implements
# first-success-wins via a per-day flag file, so only the first successful
# fire of the day does real work; the remaining fires are ~30ms no-ops.
#
# Failed runs are retried by the NEXT hour's launchd fire — there is no
# in-process retry loop here, which keeps each individual run boring
# and easy to debug.
#
# Usage:
#   ./install.sh                                  # default python auto-detect
#   ./install.sh --python /opt/homebrew/bin/python3
#
# Idempotent: re-running re-renders the plist and reloads launchd.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # _engine/_scheduler/
KB_ROOT="$(cd "$HERE/../.." && pwd)"                    # KnowledgeBase/
LABEL="com.rahul.banking-kb-daily-refresh"
TEMPLATE="$HERE/${LABEL}.plist.template"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/${LABEL}.plist"

PYTHON_BIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)  PYTHON_BIN="$2"; shift 2 ;;
    --time)
      echo "WARN: --time is no longer supported. The schedule is now fixed at" >&2
      echo "      hourly fires 10:00-15:00 local time, every day. If you really" >&2
      echo "      need a different schedule, edit StartCalendarInterval in" >&2
      echo "      $TEMPLATE  before running install.sh." >&2
      shift 2
      ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Auto-detect python3 if not supplied. Prefer the engine's own .venv (it
# has all deps including Playwright + the chromium browser).
if [[ -z "$PYTHON_BIN" ]]; then
  for cand in \
    "$KB_ROOT/_engine/.venv/bin/python3" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$(command -v python3 || true)"
  do
    if [[ -n "$cand" && -x "$cand" ]]; then PYTHON_BIN="$cand"; break; fi
  done
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "FATAL: could not find python3. Pass --python /path/to/python3" >&2; exit 2
fi

# Verify the engine's deps are importable from this python.
if ! "$PYTHON_BIN" -c 'import requests, bs4' >/dev/null 2>&1; then
  echo "WARN: '$PYTHON_BIN' is missing 'requests' or 'beautifulsoup4'."
  echo "      Install with:  $PYTHON_BIN -m pip install -r '$KB_ROOT/_engine/requirements.txt'"
  echo "      Continuing anyway — fix this before the next scheduled run."
fi

chmod +x "$HERE/run_daily.sh"
mkdir -p "$TARGET_DIR" "$KB_ROOT/_engine/_logs"

# Render template. The plist's StartCalendarInterval is fixed (hourly
# 10:00-15:00 every day) so only KB_ROOT and PYTHON_BIN need substitution.
sed \
  -e "s|__KB_ROOT__|$KB_ROOT|g" \
  -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
  "$TEMPLATE" > "$TARGET"

# Reload.
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

echo "Installed $LABEL"
echo "  plist:       $TARGET"
echo "  kb_root:     $KB_ROOT"
echo "  python_bin:  $PYTHON_BIN"
echo "  schedule:    daily 10:00 / 11:00 / 12:00 / 13:00 / 14:00 / 15:00 local time"
echo "  policy:      first success of the day wins (subsequent fires no-op via"
echo "               _engine/_logs/.success_YYYY-MM-DD.flag)"
echo
echo "Next steps:"
echo "  • Status:           ./status.sh"
echo "  • Trigger NOW:      launchctl start $LABEL   (or: ./run_daily.sh)"
echo "  • Force re-run:     FORCE=1 ./run_daily.sh   (ignores today's flag)"
echo "  • View logs:        ls -lt $KB_ROOT/_engine/_logs | head"
