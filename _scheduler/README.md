# `_engine/_scheduler/` — how refreshes are triggered

The knowledge base is refreshed **only when you ask it to.** There is no
unattended daily timer, no background daemon. One script — `refresh.sh`,
run from Cursor's terminal — is the entire workflow.

See `_engine/architecture.svg` for the diagram.

## The everyday workflow

```bash
cd /Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler

# Standard daily / backfill
./refresh.sh                                                  # daily, all banks, all types
./refresh.sh --bank Kotak_Mahindra_Bank                       # single bank, daily mode
./refresh.sh --mode backfill --bank Yes_Bank                  # 5-yr history for one bank

# Narrow to one (or several) document categories
./refresh.sh --type annual_reports                            # ARs only, every bank
./refresh.sh --bank HDFC_Bank --type investor_presentations   # HDFC decks only
./refresh.sh --mode backfill --bank ICICI_Bank --type press_releases
./refresh.sh --type annual_reports --type transcripts         # both
./refresh.sh --type annual_reports,transcripts                # same, comma form

# Pin the source URL(s) — bypass banks_config for that bank, skip NSE
./refresh.sh --bank Kotak_Mahindra_Bank \
    --url https://www.kotak.bank.in/en/investor-relations/financial-results.html
./refresh.sh --mode backfill --bank IDFC_First_Bank --type press_releases \
    --url https://www.idfcfirst.bank.in/our-history/news-media/press-releases
./refresh.sh --bank Yes_Bank --url URL_1 --url URL_2          # multiple URLs
./refresh.sh --bank Yes_Bank --url URL_1,URL_2                # same, comma form

# Utilities
./refresh.sh --status                                         # last-run state + corpus stats
./refresh.sh --sync-structure                                 # create folders for any new banks
```

Valid `--type` values (folder names from `bank_kb/structure.py:SUBFOLDERS`):
`investor_presentations`, `annual_reports`, `transcripts`, `press_releases`,
`financial_results`. Singular forms (`annual_report`, `transcript`, …) and
shorthand (`ar`, `pr`, `presentations`) are accepted too.

`--url` requires `--bank` (the engine needs to know whose folders the
downloads belong in). When set, it **replaces** the bank's configured
sources entirely and **skips NSE discovery** for that run — useful for
testing a candidate URL before committing it to `banks_config.json`, or
for pinning to a single archive page while debugging. The PDFs still land
in the normal `<Bank>/<type>/FY<N>/` folders and the classifier still runs.

`refresh.sh`:

- Activates the engine's `.venv` automatically (avoids `ModuleNotFoundError: requests`).
- Checks `requests` and `bs4` import before running — fails fast with the exact pip command if the venv is missing deps.
- Refuses to race another engine instance (e.g. if you accidentally start the LaunchAgent — see below) unless you pass `--force`.
- Prints a Cowork-style summary at the end: per-bank breakdown of new downloads, NSE noise separated from real errors, JSON log path.

Set an alias once:

```bash
alias kb='/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler/refresh.sh'
# kb                           # daily, all banks
# kb --bank Kotak_Mahindra_Bank
# kb --status
```

## Files

| File | Role |
| --- | --- |
| `refresh.sh` | **The entry point.** Manual, foreground, from Cursor's terminal. |
| `run_daily.sh` | Wrapper a LaunchAgent *would* invoke. Used only if you opt back in to the auto-schedule (see below). |
| `com.rahul.banking-kb-daily-refresh.plist.template` | macOS LaunchAgent plist with `__KB_ROOT__` / `__PYTHON_BIN__` / `__HOUR__` / `__MINUTE__` placeholders. Off by default. |
| `install.sh` | Renders the template into `~/Library/LaunchAgents/` and `launchctl load`s it. Run this **only** if you want the auto-schedule back. |
| `uninstall.sh` | `launchctl unload` and remove the plist. Currently the right thing to run if `install.sh` was used earlier. |
| `status.sh` | Shows whether the agent is loaded, last engine JSON log, last wrapper log tail. |
| `cowork_report_prompt.md` | Source-of-truth for the Cowork `banking-kb-daily-refresh` task (currently `enabled: false`). |

## Opt-in: the unattended LaunchAgent (08:30 Mon-Fri)

The macOS LaunchAgent is **not** registered by default in this checkout.
If you've been running it from an earlier setup, uninstall it:

```bash
./uninstall.sh    # launchctl unload + rm ~/Library/LaunchAgents/<label>.plist
./status.sh       # should now show "loaded: no"
```

If you ever want to re-enable the unattended daily run:

```bash
./install.sh                                 # defaults to 08:30 Mon-Fri
./install.sh --time 07:15                    # custom time
./install.sh --python /opt/homebrew/bin/python3   # custom python
```

A few macOS specifics worth knowing if you re-enable:

- LaunchAgents need the user to be logged in and the Mac awake. If the Mac
  is asleep at 08:30, launchd fires on next wake. Multi-day vacations will
  miss days; check `refresh.sh --status` when you're back.
- `/bin/bash` and the chosen `python3` binary need **Full Disk Access** for
  the agent to read files under `~/Documents`. We hit this on first install
  — see the project README's history for the diagnosis.

## Opt-in: the Cowork chat report

The Cowork scheduled task `banking-kb-daily-refresh` is currently
`enabled: false`. Re-enable from Cowork's Scheduled sidebar (or via
`mcp__scheduled-tasks__update_scheduled_task` with `enabled: true`) if you
want a daily chat summary of the freshest run log.

The task is pure read-only — it reads `_engine/_logs/run_*.json` and posts
a summary in chat. It does **not** run the engine itself. Re-enabling it
without also running `refresh.sh` (or installing the LaunchAgent) will
produce `STALE` reports whenever the log hasn't been updated recently.

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

`run.py` calls `ensure_all_banks_structure(cfg, kb_root)` at startup, so
whether you used `add_bank.py` or hand-edited `banks_config.json`, the
per-bank folder skeleton appears on the next `refresh.sh` run — and that
new bank is fetched on the same run. No script in this folder needs
editing when you add a bank.
