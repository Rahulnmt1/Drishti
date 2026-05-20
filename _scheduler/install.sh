#!/usr/bin/env bash
# Installs the banking-kb daily refresh as a macOS LaunchAgent.
#
# Usage:
#   ./install.sh                       # default: 08:30 Mon-Fri
#   ./install.sh --time 07:15          # custom run time, still Mon-Fri
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

RUN_TIME="08:30"
PYTHON_BIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --time)    RUN_TIME="$2"; shift 2 ;;
    --python)  PYTHON_BIN="$2"; shift 2 ;;
    -h|--help) sed -n '1,12p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! [[ "$RUN_TIME" =~ ^([0-1][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
  echo "FATAL: --time must be HH:MM in 24h, got '$RUN_TIME'" >&2; exit 2
fi
HOUR="${RUN_TIME%:*}"; HOUR="${HOUR#0}"; [[ -z "$HOUR" ]] && HOUR=0
MINUTE="${RUN_TIME#*:}"; MINUTE="${MINUTE#0}"; [[ -z "$MINUTE" ]] && MINUTE=0

# Auto-detect python3 if not supplied. Prefer homebrew (it's what Cursor's
# terminal typically uses on Apple-silicon Macs).
if [[ -z "$PYTHON_BIN" ]]; then
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
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

# Render template.
sed \
  -e "s|__KB_ROOT__|$KB_ROOT|g" \
  -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
  -e "s|__HOUR__|$HOUR|g" \
  -e "s|__MINUTE__|$MINUTE|g" \
  "$TEMPLATE" > "$TARGET"

# Reload.
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

echo "Installed $LABEL"
echo "  plist:       $TARGET"
echo "  kb_root:     $KB_ROOT"
echo "  python_bin:  $PYTHON_BIN"
echo "  schedule:    ${RUN_TIME} local time, Mon-Fri"
echo
echo "Next steps:"
echo "  • Check status:   ./status.sh"
echo "  • Trigger now:    launchctl start $LABEL"
echo "  • View latest:    ls -lt $KB_ROOT/_engine/_logs | head"
