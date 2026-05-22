# `_engine/_scheduler/` — how refreshes are triggered

The knowledge base is refreshed two ways:

1. **Automatically**, by a macOS LaunchAgent installed via `./install.sh`.
   Fires hourly between 10:00 and 15:00 local time, every day. The first
   fire that succeeds wins; the remaining fires that day no-op via a
   per-day flag file. Failures are retried by the next hour's fire — no
   in-process retry loop. After 15:00 the day is over; next attempt is
   tomorrow at 10:00. See [_The unattended scheduler_](#the-unattended-scheduler-installsh) below.
2. **Manually**, by `./refresh.sh` from Cursor's terminal. This is
   independent of the LaunchAgent — runs in your foreground shell, prints
   live output, doesn't read or write the per-day success flag. Useful
   for adding a new bank, debugging discovery on one URL, or doing a
   one-off backfill outside the 10:00–15:00 window.

See `_engine/architecture.svg` for the overall flow.

## The everyday workflow (`refresh.sh`)

```bash
cd /Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler

# Focus — pick the bank you're actively working on (persists across shells)
./refresh.sh focus HDFC_Bank                                  # set focus
./refresh.sh focus                                            # show focus
./refresh.sh unfocus                                          # clear focus

# Standard daily / backfill — uses focus if set, otherwise all registered banks
./refresh.sh                                                  # daily, focused bank (or all)
./refresh.sh --bank Kotak_Mahindra_Bank                       # explicit override of focus
./refresh.sh --mode backfill --bank Yes_Bank                  # 5-yr history for one bank
./refresh.sh --all-banks --mode daily                         # force every registered bank

# Per-shell focus via env var (overrides .kb_focus for one terminal)
KB_FOCUS=Axis_Bank ./refresh.sh --mode daily

# Narrow to one (or several) document categories
./refresh.sh --type press_releases                            # PRs only, focused bank
./refresh.sh --bank HDFC_Bank --type investor_presentations   # HDFC decks only
./refresh.sh --mode backfill --bank ICICI_Bank --type press_releases
./refresh.sh --type investor_presentations,press_releases     # both, comma form

# Pin the source URL(s) — bypass banks/<X>/config.json sources, skip NSE
./refresh.sh --bank Kotak_Mahindra_Bank \
    --url https://www.kotak.bank.in/en/investor-relations/financial-results.html
./refresh.sh --mode backfill --bank IDFC_First_Bank --type press_releases \
    --url https://www.idfcfirst.bank.in/our-history/news-media/press-releases
./refresh.sh --bank Yes_Bank --url URL_1 --url URL_2          # multiple URLs
./refresh.sh --bank Yes_Bank --url URL_1,URL_2                # same, comma form

# Utilities
./refresh.sh --status                                         # focus + last-run state + corpus stats
./refresh.sh --sync-structure                                 # create folders for any registered bank
```

Valid `--type` values (folder names from `bank_kb/structure.py:SUBFOLDERS`):
`investor_presentations`, `press_releases`, `financial_results`. Singular
forms (`investor_presentation`, `press_release`, `financial_result`) and
shorthand (`ip`, `pr`, `fr`, `presentations`) are accepted too. Any other
classifier output (annual_report, transcript, "other") is dropped at
classify time per `settings.json:doc_types_whitelist`.

`--url` requires `--bank` or an active focus. When set, it **replaces** the
bank's configured sources entirely and **skips NSE discovery** for that run
— useful for testing a candidate URL before committing it to
`banks/<Bank>/config.json`, or for pinning to a single archive page while
debugging. The PDFs still land in the normal `<Bank>/<type>/FY<N>/` folders
and the classifier still runs.

`refresh.sh`:

- Activates the engine's `.venv` automatically (avoids `ModuleNotFoundError: requests`).
- Checks `requests` and `bs4` import before running — fails fast with the exact pip command if the venv is missing deps.
- Refuses to race another engine instance (e.g. an ongoing LaunchAgent fire) unless you pass `--force`.
- Prints a Cowork-style summary at the end: per-bank breakdown of new downloads, NSE noise separated from real errors, JSON log path.
- Does **not** read or write the LaunchAgent's per-day success flag. So a manual `./refresh.sh` doesn't prevent the next scheduled fire from running, and a scheduled success doesn't prevent you from running `./refresh.sh` for testing.

Set an alias once:

```bash
alias kb='/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler/refresh.sh'
# kb focus HDFC_Bank         # set focus
# kb                         # daily, focused bank
# kb --mode backfill         # backfill, focused bank
# kb --status
```

## The unattended scheduler (`install.sh`)

Schedule (fixed business policy, not a knob):

| When | What |
| --- | --- |
| Every day, 10:00 / 11:00 / 12:00 / 13:00 / 14:00 / 15:00 local time | LaunchAgent fires `run_daily.sh` |
| First fire of the day that exits 0 | Engine ran successfully; writes `_logs/.success_YYYY-MM-DD.flag`; subsequent fires that day no-op via the flag |
| Any fire that exits non-zero | Flag NOT written; next hour's fire is the retry |
| After 15:00, no success yet | Day is over; next attempt is tomorrow at 10:00 |

This handles the common failure modes for IR-page scraping:

- Bank CDN cache invalidation (often during regulatory-filing windows)
- NSE rate-limits / Akamai blocks that lift after an hour
- Transient TLS handshake failures from AEM-hosted bank sites
- A laptop that's asleep at 10:00 — the 11:00 fire catches up

### Install / verify / uninstall

```bash
cd /Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler

./install.sh                                  # auto-detect python (prefers .venv)
./install.sh --python /opt/homebrew/bin/python3   # explicit python

./status.sh                                   # loaded? next fire? today's flag state?

./uninstall.sh                                # remove the agent
```

What `install.sh` does:

1. Auto-detects a usable `python3` (prefers `_engine/.venv/bin/python3` so
   Playwright + all deps are present), checks `requests` and `bs4` import.
2. Renders `com.rahul.banking-kb-daily-refresh.plist.template` →
   `~/Library/LaunchAgents/com.rahul.banking-kb-daily-refresh.plist` with
   `KB_ROOT` and `PYTHON_BIN` substituted.
3. `launchctl unload && launchctl load` to (re)register the schedule.

Idempotent — re-running re-renders the plist and reloads it.

### Triggering a run manually (without waiting for the next hour)

```bash
# Run NOW, honoring today's flag (skip if already succeeded)
./run_daily.sh
# or, equivalently:
launchctl start com.rahul.banking-kb-daily-refresh

# Force a re-run within the day (ignores today's flag)
FORCE=1 ./run_daily.sh
# or (from this _scheduler/ folder):
rm "../_logs/.success_$(date +%Y-%m-%d).flag" && ./run_daily.sh
```

### Files in this folder

| File | Role |
| --- | --- |
| `refresh.sh` | Manual entry point, foreground, from Cursor's terminal. Doesn't touch the success flag. |
| `run_daily.sh` | LaunchAgent wrapper. Reads/writes `_logs/.success_YYYY-MM-DD.flag`. On every fire (SKIP/OK/FAIL) it prunes old wrapper logs and re-renders `_logs/scheduler_status.txt`. Exit 0 means "engine succeeded OR today already done"; exit 1 means "engine failed, next hour will retry". |
| `render_status.py` | Stdlib-only Python that scans the last 7 days of `wrapper_*.log` files and writes the day×hour table to `_logs/scheduler_status.txt`. Invoked from `run_daily.sh` (end of every fire) and runnable on-demand. |
| `com.rahul.banking-kb-daily-refresh.plist.template` | macOS LaunchAgent plist with `__KB_ROOT__` and `__PYTHON_BIN__` placeholders. Schedule is hardcoded (6 fires daily, 10-15). |
| `install.sh` | Renders the template into `~/Library/LaunchAgents/`, `launchctl load`s it. |
| `uninstall.sh` | `launchctl unload` + remove the plist. |
| `status.sh` | Loaded? Today's flag state? Next fire time? Last engine + wrapper logs? **Also embeds the 7-day status table at the bottom.** |
| `cowork_report_prompt.md` | Source-of-truth for the optional Cowork chat-report task. |

### Files the wrapper reads/writes under `_engine/_logs/`

| Path | Role |
| --- | --- |
| `.success_YYYY-MM-DD.flag` | Per-day "today already done" marker. Created on first engine exit 0; older flags auto-pruned after 14 days. |
| `wrapper_YYYY-MM-DD_HHMM.log` | One per LaunchAgent fire. Header + engine output + final SKIP/OK/FAIL line. **Retained for 7 days, pruned by age** (was previously "30 most recent"; the age-based rule guarantees the 7-day status table always has data). |
| `scheduler_status.txt` | Rolling 7-day × 6-hour table of fire outcomes. Always regenerated at the end of every wrapper fire. See [Reading scheduler_status.txt](#reading-scheduler_statustxt) below. |
| `run_YYYY-MM-DD_HHMM.json` | Engine's own JSON run summary. Written by `run.py`. Kept indefinitely. |
| `launchd_stdout.log` / `launchd_stderr.log` | launchd's raw capture. Useful only when the wrapper itself didn't get far enough to write its own log. |

### Reading `scheduler_status.txt`

A 7-day × 6-hour grid showing what happened at each scheduled fire time.
Cell codes:

| Code | Meaning |
| --- | --- |
| `OK` | Engine ran and exit 0. Whichever fire of the day got the first `OK` wins; the rest of that day's fires `SKIP` via the flag. |
| `SK` | Fire happened but the engine was skipped because today's flag was already present. |
| `FL` | Engine ran and exit non-zero (or wrapper preflight failed). The next hourly fire is the actual retry. |
| `.` | No fire recorded at this hour. Means one of: Mac was asleep/off, LaunchAgent wasn't loaded yet, the day predates installation, or this hour hasn't arrived yet (today's future hours render as blank, not `.`). |

Read three things at a glance:

1. The **First success** column shows the timestamp of each day's first
   successful fire — `10:00` is the happy path, anything later means at
   least one hourly retry was needed.
2. A row of all `FL` (or `FL FL FL FL FL FL`) is a catastrophic day —
   something systemic (bank CDN, NSE, network) was wrong all day. Look at
   the wrapper log for any fire on that day for the actual error.
3. A row of all `.` means **nothing fired that day** — usually a sleeping
   Mac. If it appears mid-week unexpectedly, check `caffeinate` settings
   and System Settings → Sleep schedule.

To render on-demand without waiting for the next wrapper fire:

```bash
./_scheduler/render_status.py            # writes _logs/scheduler_status.txt and prints
```

Or just `cat _engine/_logs/scheduler_status.txt`.

### macOS specifics worth knowing

- LaunchAgents need the user to be logged in and the Mac awake. If the Mac
  is asleep at 10:00, launchd fires once on next wake. Multi-day vacations
  will skip days; check `./status.sh` when you're back.
- `/bin/bash` and the chosen `python3` binary need **Full Disk Access** for
  the agent to read files under `~/Documents`. Grant it once via
  System Settings → Privacy & Security → Full Disk Access.
- launchd uses **local time**, which is IST for this Mac. India doesn't
  observe DST, so the 10:00–15:00 window is stable year-round.

## Opt-in: the Cowork chat report

The Cowork scheduled task `banking-kb-daily-refresh` is currently
`enabled: false`. Re-enable from Cowork's Scheduled sidebar (or via
`mcp__scheduled-tasks__update_scheduled_task` with `enabled: true`) if you
want a daily chat summary of the freshest run log.

The task is pure read-only — it reads `_engine/_logs/run_*.json` and posts
a summary in chat. It does **not** run the engine itself.

When the prompt text needs to change, edit both `cowork_report_prompt.md`
(the source-of-truth committed in this folder) **and** the Cowork task
itself via `mcp__scheduled-tasks__update_scheduled_task`. The two are
intentionally separate — repo holds the spec, Cowork holds the live copy.

## Why fetch lives on the Mac and not in Cowork

Two sandbox limits in Cowork make the engine unworkable there:

1. **Egress allowlist.** Bank/NSE domains aren't on Cowork's allowlist — every HTTPS request comes back `X-Proxy-Error: blocked-by-allowlist`. We could add the 24 domains in Settings → Capabilities (see `_engine/ALLOWLIST_REQUIRED.txt`), but that's a maintenance treadmill against bank CDN changes.
2. **45-second bash timeout.** Cowork bash calls die at 45 s and background processes are killed between calls. A 10-minute engine run can't complete.

So the engine runs in your Mac's regular shell. The Cowork side, when
enabled, is purely a passive reader of the logs the Mac produces.

## Adding banks works without touching any of this

`run.py` calls `ensure_bank_structure()` for every registered bank at
startup, so whether you used `add_bank.py` or hand-created
`banks/<Bank>/config.json` directly, the per-bank corpus folders appear on
the next `refresh.sh` run — and that new bank is fetched on the same run.
No script in this folder needs editing when you add a bank.
