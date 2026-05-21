#!/usr/bin/env bash
# One-stop refresh from Cursor's terminal.
#
# This is the script you run on your Mac when you want a refresh "right now"
# without leaving the terminal. It invokes the same engine the scheduled
# LaunchAgent uses, with the same args, but it runs in your foreground shell
# so:
#   • you see progress live
#   • the network has Cursor terminal's permissions (no Cowork sandbox)
#   • Python uses the engine's .venv (no TCC/dep surprises)
#   • a Cowork-style summary prints at the end
#
# Closes the "I have to switch tools to trigger a fetch" gap. The Cowork
# scheduled task continues to read whatever JSON log this script produces,
# so anything you do here also surfaces in tomorrow morning's Cowork report.
#
# Usage:
#   ./refresh.sh                                              # daily, focused bank (or all if no focus)
#   ./refresh.sh --bank Kotak_Mahindra_Bank                   # explicit single-bank, ignores focus
#   ./refresh.sh --mode backfill                              # full 5-yr history
#   ./refresh.sh --mode backfill --bank Yes_Bank              # full 5-yr history, one bank
#   ./refresh.sh --status                                     # last-run state + corpus stats + focus, no fetch
#   ./refresh.sh --sync-structure                             # create folders for registered banks, no fetch
#   ./refresh.sh --all-banks                                  # force-run every registered bank even if focused
#
# Per-bank focus — when you're actively working on one bank and don't want
# to retype --bank every time. Persists across shells via .kb_focus, can be
# overridden per-terminal via env var KB_FOCUS:
#   ./refresh.sh focus HDFC_Bank                              # set focus
#   ./refresh.sh focus                                        # show current focus
#   ./refresh.sh unfocus                                      # clear focus
#   ./refresh.sh                                              # uses focused bank
#   KB_FOCUS=Axis_Bank ./refresh.sh                           # env overrides .kb_focus for one shell
#
# Fetch one document category at a time (any combination of bank + mode):
#   ./refresh.sh --type investor_presentations                # IPs only
#   ./refresh.sh --bank HDFC_Bank --type investor_presentations
#   ./refresh.sh --mode backfill --bank ICICI_Bank --type press_releases
#   ./refresh.sh --type investor_presentations,press_releases # multiple types, comma form
#
# Valid --type values (folder names you see on disk, or singular forms):
#   investor_presentations, press_releases, financial_results
#   (singular: investor_presentation, press_release, financial_result)
#
# Pin the source URL(s) instead of using the bank's config.json (good for
# testing a new IR page before committing it). Requires --bank or focus.
# NSE is skipped automatically when --url is set:
#   ./refresh.sh --bank Kotak_Mahindra_Bank \
#                --url https://www.kotak.bank.in/en/investor-relations/financial-results.html
#   ./refresh.sh --bank Yes_Bank --url URL_1 --url URL_2     # multiple
#   ./refresh.sh --bank Yes_Bank --url URL_1,URL_2           # same, comma form
#
# Tip — alias it once in your shell rc:
#   alias kb='/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler/refresh.sh'
# Then:   kb focus HDFC_Bank ; kb ; kb --status

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # _engine/_scheduler
ENGINE_DIR="$(cd "$HERE/.." && pwd)"                    # _engine
KB_ROOT="$(cd "$ENGINE_DIR/.." && pwd)"                 # KnowledgeBase
LOG_DIR="$ENGINE_DIR/_logs"
FOCUS_FILE="$ENGINE_DIR/.kb_focus"
LABEL="com.rahul.banking-kb-daily-refresh"
DEFAULT_PYTHON="$ENGINE_DIR/.venv/bin/python3"

# ---- focus subcommand handling (must run BEFORE arg parsing) --------------
# `./refresh.sh focus [<Bank>]` and `./refresh.sh unfocus` are positional
# subcommands; they short-circuit the normal flag-driven path.

case "${1:-}" in
  focus)
    if [[ $# -eq 1 ]]; then
      # No bank name => show current focus.
      if [[ -n "${KB_FOCUS:-}" ]]; then
        echo "Focused bank: $KB_FOCUS   [via env KB_FOCUS — wins over $FOCUS_FILE]"
      elif [[ -s "$FOCUS_FILE" ]]; then
        echo "Focused bank: $(cat "$FOCUS_FILE")   [from $FOCUS_FILE]"
      else
        echo "Focused bank: <none>"
        echo "Set with:  $0 focus <BankName>"
      fi
      exit 0
    fi
    if [[ $# -ne 2 ]]; then
      echo "usage: $0 focus [<BankName>]" >&2
      exit 2
    fi
    BANK_TO_FOCUS="$2"
    # Sanity-check that the bank actually exists under banks/, so we don't
    # silently focus on a non-existent or misspelled name.
    if [[ ! -d "$ENGINE_DIR/banks/$BANK_TO_FOCUS" ]]; then
      echo "ERROR: no bank named '$BANK_TO_FOCUS' under $ENGINE_DIR/banks/." >&2
      echo "Available:" >&2
      for d in "$ENGINE_DIR"/banks/*/; do
        [[ -d "$d" ]] || continue
        nm="$(basename "$d")"
        [[ "$nm" == "_template" ]] && continue
        echo "  - $nm" >&2
      done
      echo "Add a new bank with:  python3 $ENGINE_DIR/add_bank.py $BANK_TO_FOCUS" >&2
      exit 2
    fi
    echo "$BANK_TO_FOCUS" > "$FOCUS_FILE"
    echo "Focused bank: $BANK_TO_FOCUS  (written to $FOCUS_FILE)"
    if [[ -n "${KB_FOCUS:-}" && "$KB_FOCUS" != "$BANK_TO_FOCUS" ]]; then
      echo "Note: KB_FOCUS=$KB_FOCUS is set in this shell and will override the file"
      echo "      for commands run here. Unset it with:  unset KB_FOCUS"
    fi
    exit 0
    ;;
  unfocus)
    if [[ -f "$FOCUS_FILE" ]]; then
      rm "$FOCUS_FILE"
      echo "Cleared focus (removed $FOCUS_FILE)."
    else
      echo "No focus file to clear."
    fi
    if [[ -n "${KB_FOCUS:-}" ]]; then
      echo "Warning: KB_FOCUS=$KB_FOCUS is still set in this shell — unset it with: unset KB_FOCUS"
    fi
    exit 0
    ;;
esac

# ---- args ------------------------------------------------------------------

MODE="daily"
BANK=""
ALL_BANKS=false
STATUS_ONLY=false
SYNC_ONLY=false
FORCE=false
PYTHON_BIN="$DEFAULT_PYTHON"
TYPES=()    # collected via --type (one or many)
URLS=()     # collected via --url (one or many, requires --bank or focus)

usage() {
  # Print the leading comment block (everything up to the first blank line).
  awk '/^$/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)            MODE="$2"; shift 2 ;;
    --bank)            BANK="$2"; shift 2 ;;
    --all-banks)       ALL_BANKS=true; shift ;;
    --type)            TYPES+=("$2"); shift 2 ;;
    --url)             URLS+=("$2"); shift 2 ;;
    --status)          STATUS_ONLY=true; shift ;;
    --sync-structure)  SYNC_ONLY=true; shift ;;
    --python)          PYTHON_BIN="$2"; shift 2 ;;
    --force)           FORCE=true; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# Resolve effective focus for display + URL-override validation. Precedence:
# explicit --bank > KB_FOCUS env var > .kb_focus file > none.
RESOLVED_FOCUS=""
RESOLVED_FOCUS_SOURCE=""
if [[ -n "$BANK" ]]; then
  RESOLVED_FOCUS="$BANK"
  RESOLVED_FOCUS_SOURCE="--bank flag"
elif [[ -n "${KB_FOCUS:-}" ]]; then
  RESOLVED_FOCUS="$KB_FOCUS"
  RESOLVED_FOCUS_SOURCE="env KB_FOCUS"
elif [[ -s "$FOCUS_FILE" ]]; then
  RESOLVED_FOCUS="$(cat "$FOCUS_FILE")"
  RESOLVED_FOCUS_SOURCE=".kb_focus"
fi

# Early validation: --url needs a single bank scope.
if [[ ${#URLS[@]} -gt 0 && -z "$RESOLVED_FOCUS" ]]; then
  echo "FATAL: --url requires --bank or a focused bank ('$0 focus <Bank>')." >&2
  exit 2
fi

# ---- preflight -------------------------------------------------------------

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "FATAL: python not executable at $PYTHON_BIN" >&2
  echo "Hint:  ensure the venv exists ($ENGINE_DIR/.venv) or pass --python /path/to/python3" >&2
  exit 2
fi

if ! "$PYTHON_BIN" -c 'import requests, bs4' >/dev/null 2>&1; then
  echo "FATAL: '$PYTHON_BIN' is missing required deps (requests/beautifulsoup4)." >&2
  echo "Hint:  $PYTHON_BIN -m pip install -r '$ENGINE_DIR/requirements.txt'" >&2
  exit 2
fi

# Collision check: don't fight the LaunchAgent for the SQLite index.
if [[ "$STATUS_ONLY" == false && "$SYNC_ONLY" == false && "$FORCE" == false ]]; then
  if launchctl list 2>/dev/null | grep -qE "^[0-9]+[[:space:]]+[0-9]+[[:space:]]+$LABEL$"; then
    echo "⚠  LaunchAgent '$LABEL' is currently running."
    echo "   Running another engine instance now will race on _engine/kb_index.sqlite."
    echo "   Either wait for it to finish, run with --force, or:"
    echo "     launchctl stop $LABEL"
    exit 3
  fi
fi

# ---- non-fetch paths -------------------------------------------------------

cd "$ENGINE_DIR"

if $STATUS_ONLY; then
  exec "$PYTHON_BIN" run.py --status
fi

if $SYNC_ONLY; then
  exec "$PYTHON_BIN" run.py --sync-structure
fi

# ---- fetch path ------------------------------------------------------------

echo "=== banking-kb refresh ==="
echo "  mode:        $MODE"
if [[ -n "$RESOLVED_FOCUS" ]]; then
  if $ALL_BANKS; then
    echo "  scope:       <all banks>   (--all-banks; ignoring focus '$RESOLVED_FOCUS' from $RESOLVED_FOCUS_SOURCE)"
  else
    echo "  scope:       $RESOLVED_FOCUS   [from $RESOLVED_FOCUS_SOURCE]"
  fi
else
  echo "  scope:       <all registered banks>   (no --bank / focus)"
fi
if [[ ${#TYPES[@]} -gt 0 ]]; then
  echo "  types:       ${TYPES[*]}"
else
  echo "  types:       <settings.json:doc_types_whitelist applies>"
fi
if [[ ${#URLS[@]} -gt 0 ]]; then
  echo "  url override:  ${URLS[*]}"
  echo "                 (NSE discovery skipped; banks/$RESOLVED_FOCUS/config.json sources ignored)"
else
  echo "  url override:  <none — using banks/<bank>/config.json>"
fi
echo "  python_bin:  $PYTHON_BIN"
echo "  kb_root:     $KB_ROOT"
echo "  started_at:  $(date -Iseconds)"
echo ""

CMD=("$PYTHON_BIN" run.py --mode "$MODE" --verbose)
if [[ -n "$BANK" ]]; then
  CMD+=(--bank "$BANK")
fi
if $ALL_BANKS; then
  CMD+=(--all-banks)
fi
# Note: bash 3.2 (default on macOS) with `set -u` errors on "${arr[@]}" when
# the array is empty. Guard the iterations with a length check.
if [[ ${#TYPES[@]} -gt 0 ]]; then
  for t in "${TYPES[@]}"; do
    CMD+=(--type "$t")
  done
fi
if [[ ${#URLS[@]} -gt 0 ]]; then
  for u in "${URLS[@]}"; do
    CMD+=(--url "$u")
  done
fi

"${CMD[@]}"
RC=$?

echo ""
echo "  engine_exit: $RC"
echo "  finished_at: $(date -Iseconds)"

# ---- Cowork-style summary --------------------------------------------------
# Reuses exactly the parsing logic the Cowork scheduled task uses so what you
# see here matches what shows up in chat tomorrow morning.

LATEST=$(ls -1t "$LOG_DIR"/run_*.json 2>/dev/null | head -1 || true)
if [[ -n "$LATEST" ]]; then
  echo ""
  echo "=== summary (from $LATEST) ==="
  "$PYTHON_BIN" - "$LATEST" <<'PY'
import json, sys
from pathlib import Path

log = json.loads(Path(sys.argv[1]).read_text())
banks = log.get("banks", [])

totals = {k: sum(b.get(k, 0) for b in banks)
          for k in ("discovered", "in_window", "new_downloads",
                    "skipped_existing", "skipped_by_type",
                    "download_failures", "extract_failures")}
print(f"focus:           {log.get('focus') or '<none>'}")
print(f"banks processed: {len(banks)}")
print(f"new downloads:   {totals['new_downloads']}")
print(f"skipped (seen):  {totals['skipped_existing']}")
print(f"skipped (type):  {totals.get('skipped_by_type', 0)}"
      f"   <-- dropped before download (not in doc_types_whitelist)")
print(f"dl failures:     {totals['download_failures']}")
print(f"extract fails:   {totals['extract_failures']}")

nonzero = [b for b in banks if b.get("new_downloads", 0) > 0]
if nonzero:
    print()
    print("Per-bank new downloads:")
    for b in sorted(nonzero, key=lambda x: -x.get("new_downloads", 0)):
        print(f"  {b['bank']:30s} +{b['new_downloads']}")

all_errors = [(b["bank"], e) for b in banks for e in b.get("errors", [])]
non_nse = [(bk, e) for bk, e in all_errors if "nseindia" not in e.lower() and "nse" not in e.lower()]
nse = [(bk, e) for bk, e in all_errors if (bk, e) not in non_nse]

if non_nse:
    print()
    print("Non-NSE errors (worth investigating):")
    for bank, err in non_nse[:25]:
        print(f"  [{bank}] {err[:200]}")
    if len(non_nse) > 25:
        print(f"  ... +{len(non_nse) - 25} more")

if nse:
    print()
    print(f"NSE errors: {len(nse)} (typical — NSE rate-limits aggressive UAs;")
    print("            engine falls through to IR scraping automatically)")

fatals = [(b["bank"], b.get("fatal_error")) for b in banks if b.get("fatal_error")]
if fatals:
    print()
    print("FATAL crashes:")
    for bank, err in fatals:
        print(f"  [{bank}] {err}")
PY
fi

exit "$RC"
