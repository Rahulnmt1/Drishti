#!/usr/bin/env python3
"""Render a 7-day x 6-hour scheduler status table from wrapper_*.log files.

Source of truth: every fire of `run_daily.sh` writes a
`wrapper_YYYY-MM-DD_HHMM.log` under `_engine/_logs/`. This script scans
those, parses the terminal `OK:` / `SKIP:` / `FAIL:` / `FATAL:` line, and
renders a tabular `_logs/scheduler_status.txt` for at-a-glance health.

Designed to be invoked from `run_daily.sh` at the end of every fire — no
deps outside stdlib, so it runs wherever the engine itself runs.

Output format (sample):

    Banking-KB Daily Refresh — scheduler status (last 7 days)
    Generated: 2026-05-22 10:50 IST  ·  Schedule: 10:00-15:00 daily, first-success-wins

    Status codes:
      OK  engine ran and succeeded
      SK  fire skipped (today already succeeded)
      FL  engine ran and failed (next hourly fire retries)
      .   no fire recorded at this hour (Mac asleep, agent unloaded, before install)

    Date         Day   10:00   11:00   12:00   13:00   14:00   15:00  │  First success
    -------------------------------------------------------------------------------
    2026-05-22   Fri    OK      SK      SK      SK                    │  10:00:14
    2026-05-21   Thu    FL      OK      SK      SK      SK      SK    │  11:01:33
    ...

    Summary (last 7 days):  6 successful · 0 pending today · 1 day with no fires
    Wrapper logs retained: 7 days  ·  This file regenerated on every wrapper fire.

Hour bucketing rule: if multiple wrapper logs exist within the same hour
(e.g. scheduled 10:00 fire + a manual `FORCE=1 ./run_daily.sh` at 10:35),
the EARLIEST one wins — that's the scheduled fire we actually want to
visualize, not the operator's debug run.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path


SCHEDULE_HOURS = (10, 11, 12, 13, 14, 15)
WINDOW_DAYS = 7
WRAPPER_NAME_RE = re.compile(r"^wrapper_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})\.log$")

# Column shape: one leading space + 5-char right-aligned value = 6 chars per
# column. Values are "10:00" / "OK" / "SK" / "FL" / "." / "" so 5 chars fits.
def _hour_header(hour: int) -> str:
    return f" {('%02d:00' % hour):>5}"

def _cell(value: str) -> str:
    return f" {value:>5}"

CELL_OK = _cell("OK")
CELL_SK = _cell("SK")
CELL_FL = _cell("FL")
CELL_NONE = _cell(".")
CELL_FUTURE = _cell("")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_wrapper_log(path: Path) -> str:
    """Return one of: 'OK', 'SKIP', 'FAIL'. Falls back to 'FAIL' on malformed logs."""
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "FAIL"
    if "OK: wrote " in body:
        return "OK"
    if "SKIP: today already succeeded" in body:
        return "SKIP"
    if "FAIL: engine exit=" in body or "FATAL:" in body:
        return "FAIL"
    # Wrapper started but never wrote a terminal marker — likely killed mid-run.
    # Treat as FAIL so the operator notices.
    return "FAIL"


def scan_wrapper_logs(log_dir: Path) -> dict[tuple[str, int], tuple[str, str]]:
    """Map `(date_str, hour_int) -> (HHMM_str, status_str)` for the EARLIEST
    log in each hour.

    Why earliest: a scheduled 10:00 fire produces `wrapper_2026-05-22_1000.log`;
    a later `FORCE=1 ./run_daily.sh` at 10:35 produces `_1035.log`. The 10:00
    cell should reflect the scheduled fire, not the operator's debug run.
    """
    by_hour: dict[tuple[str, int], tuple[str, str]] = {}
    for path in log_dir.glob("wrapper_*.log"):
        m = WRAPPER_NAME_RE.match(path.name)
        if not m:
            continue
        date_str, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        hhmm = f"{hh:02d}:{mm:02d}"
        key = (date_str, hh)
        status = parse_wrapper_log(path)
        prev = by_hour.get(key)
        if prev is None or hhmm < prev[0]:
            by_hour[key] = (hhmm, status)
    return by_hour


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _tz_label(when: dt.datetime) -> str:
    """Best-effort local timezone label (e.g. 'IST'). Empty if not derivable."""
    name = when.astimezone().tzname()
    return name or ""


def render(log_dir: Path,
           today: dt.date | None = None,
           now: dt.datetime | None = None) -> str:
    today = today or dt.date.today()
    now = now or dt.datetime.now()
    by_hour = scan_wrapper_logs(log_dir)
    days = [today - dt.timedelta(days=i) for i in range(WINDOW_DAYS)]

    code_to_cell = {
        "OK": CELL_OK,
        "SKIP": CELL_SK,
        "FAIL": CELL_FL,
    }

    lines: list[str] = []
    lines.append("Banking-KB Daily Refresh — scheduler status (last 7 days)")
    tz = _tz_label(now)
    tz_suffix = f" {tz}" if tz else ""
    lines.append(
        f"Generated: {now:%Y-%m-%d %H:%M}{tz_suffix}"
        "  ·  Schedule: 10:00-15:00 daily, first-success-wins"
    )
    lines.append("")
    lines.append("Status codes:")
    lines.append("  OK  engine ran and succeeded")
    lines.append("  SK  fire skipped (today already succeeded)")
    lines.append("  FL  engine ran and failed (next hourly fire retries)")
    lines.append("  .   no fire recorded at this hour (Mac asleep, agent unloaded, before install)")
    lines.append("")

    # Header — fixed-width Date/Day prefix, then 6-char-wide hour columns.
    prefix = f"{'Date':<10}  {'Day':<4}"
    header = prefix + "".join(_hour_header(h) for h in SCHEDULE_HOURS) + "  │  First success"
    lines.append(header)
    lines.append("-" * len(header))

    # Body
    days_ok = 0
    days_pending = 0
    days_no_fires = 0
    for d in days:
        day_short = d.strftime("%a")
        row = f"{d.isoformat():<10}  {day_short:<4}"
        first_ok: str | None = None
        any_fire = False
        for h in SCHEDULE_HOURS:
            key = (d.isoformat(), h)
            entry = by_hour.get(key)
            if entry is None:
                # Today's not-yet-fired hours show blank, not "."
                if d == today and h > now.hour:
                    cell = CELL_FUTURE
                else:
                    cell = CELL_NONE
            else:
                hhmm_str, status = entry
                cell = code_to_cell.get(status, CELL_FL)
                any_fire = True
                if status == "OK" and first_ok is None:
                    # Use the recorded HHMM (which includes minutes) for the
                    # "first success" column — more informative than just "10".
                    first_ok = hhmm_str
            row += cell
        # Trailing note
        if first_ok is not None:
            note = first_ok
            days_ok += 1
        elif d == today and now.hour <= 15:
            note = "(today, still pending)"
            days_pending += 1
        elif not any_fire:
            note = "—  (no fires recorded)"
            days_no_fires += 1
        else:
            note = "—  (all fires failed)"
        row += f"  │  {note}"
        lines.append(row)

    lines.append("")
    days_no_fires_phrase = (
        f"{days_no_fires} day with no fires"
        if days_no_fires == 1
        else f"{days_no_fires} days with no fires"
    )
    lines.append(
        f"Summary (last {WINDOW_DAYS} days):  "
        f"{days_ok} successful · "
        f"{days_pending} pending today · "
        f"{days_no_fires_phrase}"
    )
    lines.append("Wrapper logs retained: 7 days  ·  This file regenerated on every wrapper fire.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv:
        log_dir = Path(argv[0])
    else:
        # Default: _engine/_logs/ relative to this script.
        log_dir = Path(__file__).resolve().parent.parent / "_logs"
    if not log_dir.is_dir():
        print(f"log dir not found: {log_dir}", file=sys.stderr)
        return 1
    out = render(log_dir)
    target = log_dir / "scheduler_status.txt"
    target.write_text(out, encoding="utf-8")
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
