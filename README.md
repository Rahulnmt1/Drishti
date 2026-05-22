# Indian Banking Knowledge Base Engine

A pipeline that mirrors **investor presentations**, **press releases**, and
**financial results** for Indian banks (and any BFSI entity you add), then
makes the corpus searchable from the command line — so when you walk into a
CXO meeting at HDFC or SBI, you can ground the conversation in their own
strategic narrative instead of generic AI/banking talking points.

The engine also handles non-bank BFSI customers (NBFCs, insurers, fintechs) —
anything you can configure via `add_bank.py`. The word "bank" in the code is
just the entity label.

## What's tracked, what isn't

Every bank you add gets these document types fetched and indexed:

| Type                       | Folder                       | Why kept |
| -------------------------- | ---------------------------- | -------- |
| `investor_presentation`    | `investor_presentations/`    | Quarterly decks — the highest-density signal of the CXO's narrative |
| `financial_result`         | `investor_presentations/` (aliased) | Quarterly result PDFs (statements of profit, balance sheet snapshot) |
| `press_release`            | `press_releases/`            | Newsroom + quarterly-result PRs |

Annual reports, earnings-call transcripts, and "other" (footer junk, ESG decks,
investor brochures, etc.) are **explicitly dropped** at classify time — they're
noisy and rarely searched. Adjust this via `settings.json:doc_types_whitelist`
if you ever need to expand.

## Per-bank workspace, not monolithic config

Each bank lives in its own folder under `banks/<BankName>/`. The first one
shipped in this repo is HDFC_Bank — it serves as the worked example
throughout this doc:

```
banks/HDFC_Bank/
├── config.json    ← ticker, IR sources, requires_js, use_nse, optional per-bank doc_types
├── notes.md       ← human troubleshooting log (HDFC's exact page structure + why the adapter exists)
└── adapter.py     ← custom Python: keep only IP/PR rows from HDFC's aria-labelled grid
```

See [`banks/README.md`](banks/README.md) for the full per-bank file shape and
adapter contract, and [`banks/HDFC_Bank/notes.md`](banks/HDFC_Bank/notes.md)
for a real example of what a populated notes file looks like. The starting
state otherwise ships with zero registered banks; add them one at a time
with `python3 add_bank.py <Name>` as you onboard each.

---

## Contents

1.  [Layout on disk](#layout-on-disk)
2.  [First-time setup](#first-time-setup)
3.  [End-to-end pilot on one bank](#end-to-end-pilot-on-one-bank)
4.  [Verifying which config the engine loaded](#verifying-which-config-the-engine-loaded)
5.  [Refreshing the KB](#refreshing-the-kb)
6.  [Searching the corpus](#searching-the-corpus)
7.  [Adding a new bank or customer](#adding-a-new-bank-or-customer)
8.  [How auto-discovery works](#how-auto-discovery-works)
9.  [Command reference (cheat sheet)](#command-reference-cheat-sheet)
10. [Troubleshooting](#troubleshooting)
11. [Files & data hygiene](#files--data-hygiene)

---

## Layout on disk

```
KnowledgeBase/
├── _engine/                 ← code + per-bank configs + index + scheduler + logs
│   ├── bank_kb/             # Python package
│   │   ├── fetcher.py       # polite HTTP client (retries, throttle, size cap)
│   │   ├── discover.py      # extract <a *.pdf> from any IR page
│   │   ├── classify.py      # label type (IP / PR / financial_result) + fiscal period
│   │   ├── extractor.py     # PDF → text + 12 topic flags
│   │   ├── indexer.py       # SQLite FTS5 index + manifest (dedup)
│   │   ├── nse_source.py    # NSE corporate-announcements API client
│   │   ├── js_fetcher.py    # Playwright headless render + native <select> year iteration
│   │   ├── structure.py     # per-bank folder skeleton (IP + PR + extracted_text)
│   │   ├── registry.py      # loads settings.json + banks/*/config.json + per-bank adapters
│   │   └── orchestrator.py  # per-bank: discover → download → extract → index
│   ├── banks/                                          # ← per-bank workspaces
│   │   ├── README.md                                   #   how the per-bank shape works
│   │   ├── _template/                                  #   skeleton copied by add_bank.py
│   │   │   ├── config.json
│   │   │   ├── notes.md
│   │   │   └── adapter.py.example
│   │   └── <BankName>/                                 #   one per bank you add
│   │       ├── config.json   (REQUIRED — sources, ticker, requires_js, etc.)
│   │       ├── notes.md      (RECOMMENDED — troubleshooting log)
│   │       └── adapter.py    (OPTIONAL — bank-specific render/classify overrides)
│   ├── settings.json        # GLOBAL settings (UA, delays, history_years, doc_types_whitelist)
│   ├── .kb_focus            # per-machine: currently-focused bank (gitignored)
│   ├── _logs/               # JSON run summaries + wrapper/launchd logs
│   ├── _logs_archive/       # archives of pre-refactor _logs/ (gitignored)
│   ├── architecture.svg     # diagram of the manual-only flow
│   ├── _scheduler/          # manual refresh.sh + LaunchAgent (auto-on) + Cowork prompt (opt-in)
│   │   ├── refresh.sh                                  # ← manual entry point. `focus` / `unfocus` subcommands.
│   │   ├── run_daily.sh                                # LaunchAgent wrapper. Reads/writes the per-day success flag.
│   │   ├── com.rahul.banking-kb-daily-refresh.plist.template   # fires hourly 10-15 every day (fixed schedule)
│   │   ├── install.sh / uninstall.sh / status.sh       # LaunchAgent lifecycle
│   │   ├── cowork_report_prompt.md                     # report prompt (Cowork task is `enabled: false`)
│   │   └── README.md
│   ├── kb_index.sqlite      # FTS5 search index (created on first run, gitignored)
│   ├── requirements.txt
│   ├── run.py               # backfill/daily orchestrator + --status + --sync-structure
│   ├── add_bank.py          # onboard new banks/customers (scaffolds banks/<Name>/)
│   ├── query.py             # CLI search
│   ├── ALLOWLIST_REQUIRED.txt  # only relevant if you ever run fetch inside Cowork
│   └── README.md            # this file
├── HDFC_Bank/                                          # ← bank corpus folders (siblings of _engine/)
│   ├── investor_presentations/
│   │   ├── FY2024/q{1,2,3,4}fy24-earnings-presentation.pdf       (4 — via CDN probe; not on IR page)
│   │   ├── FY2025/{Q1,Q2,Q3,q4}fy25-*-presentation.pdf            (4 — via IR-page scrape)
│   │   └── FY2026/{q1,q2,Q3,q4}fy26-earnings-presentation.pdf     (4 — via IR-page scrape)
│   ├── press_releases/
│   │   ├── FY2024/press-release-to-announce-financial-results-*.pdf  (4 quarters)
│   │   ├── FY2025/press-release-to-announce-financial-results-*.pdf  (4 quarters)
│   │   └── FY2026/press-release-{june,september,december,march}-*.pdf
│   └── extracted_text/*.txt
└── ... (one folder per bank you've added)
```

### What changed from earlier versions

The earlier monolithic `banks_config.json` and 5-folder skeleton
(`annual_reports/`, `transcripts/`, ...) have been retired. The new layout
puts each bank's config and quirks in `banks/<Bank>/`, narrows the on-disk
folder shape to just IP + PR + `extracted_text`, and provides an optional
per-bank `adapter.py` for the long tail of banks (HDFC, ICICI, SBI, …) that
render their IR pages with custom JS widgets the generic engine can't drive.

---

## First-time setup

Open Terminal and run these once:

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"

# 1. (Recommended) Create a virtualenv so the engine has its own dependency space.
python3 -m venv .venv
source .venv/bin/activate

# 2. Install Python dependencies.
python3 -m pip install -r requirements.txt

# 3. Install Playwright's headless Chromium browser.
#    This is what lets the engine handle JS-rendered IR pages (HDFC, ICICI, SBI)
#    automatically — no manual URL maintenance.
python3 -m playwright install chromium
```

Requires Python 3.10+ on macOS.

If you ever open a new Terminal window, re-activate the virtualenv first:

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"
source .venv/bin/activate
```

---

## End-to-end pilot on one bank

The engine ships with **HDFC_Bank** already registered as the worked example;
that's also a good first read for how a per-bank workspace with a custom
adapter actually looks. New banks get added one at a time — see
[Adding a new bank](#adding-a-new-bank-or-customer). The HDFC flow that's
already wired up:

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"
source .venv/bin/activate

# 1. Focus the bank so subsequent commands implicitly scope to it.
./_scheduler/refresh.sh focus HDFC_Bank

# 2. Full 5-year backfill (HDFC currently publishes 3 years — FY24/25/26 —
#    on their IR page; the engine just takes what's exposed).
./_scheduler/refresh.sh --mode backfill

# 3. Confirm docs landed and are indexed.
./_scheduler/refresh.sh --status

# 4. Sanity-check searchability.
python3 query.py "net interest margin" --bank HDFC_Bank --type investor_presentation --limit 5
python3 query.py "profit after tax" --bank HDFC_Bank --type press_release --limit 5

# 5. Run --mode daily to confirm dedup works (should show new_downloads=0).
./_scheduler/refresh.sh --mode daily
```

What that does end-to-end for HDFC:

1. Loads `banks/HDFC_Bank/{config.json, notes.md, adapter.py}` from the
   registry. The config sets `requires_js: true` and **`use_nse: false`**
   (HDFC's IR page is curated + exhaustive, so NSE just adds noise — see
   below).
2. Creates / heals `../HDFC_Bank/{investor_presentations, press_releases,
   extracted_text}/`.
3. Invokes `banks/HDFC_Bank/adapter.render_page()` instead of the generic
   Playwright renderer. The adapter does two things in one call:
   - **Scrape**: loads HDFC's IR page once (no year-tab clicks — all 3
     linked years are pre-rendered in the DOM as CSS show/hide), filters
     anchors to only those with `aria-label="Download Investor
     Presentations"` or `"Download Press Releases"`.
   - **CDN probe**: for any fiscal year in the engine's `history_years`
     window that didn't get an IP from step 1, speculatively HEADs HDFC's
     predictable CDN URL pattern (`/financial-results/<Y>-<Y>/quarter-N/q{N}fy{YY}-earnings-presentation.pdf`)
     and adds any that respond 200. This recovers the 4 FY24 IPs that
     HDFC keeps on their CDN but unlinked from the current IR layout.
   - Returns one synthetic HTML blob per fiscal year (FY26/FY25/FY24).
4. The engine treats the adapter's output as **authoritative** — it does
   NOT fall back to the static-HTML scrape (which would re-inject the
   filtered-out Key Parameters / Call Transcript / Financial Result rows).
   This behavior is gated by an `adapter_used` flag inside the orchestrator.
5. Each kept PDF gets downloaded into `HDFC_Bank/<type>/FY<N>/`, text
   extracted into `HDFC_Bank/extracted_text/`, indexed into
   `kb_index.sqlite`.
6. Writes a run summary to `_engine/_logs/run_YYYY-MM-DD_HHMM.json`.

> **Coverage note**: HDFC IPs are recoverable for FY24/FY25/FY26 only.
> FY22 and FY23 IPs only exist on NSE's corporate-announcements feed,
> which is currently network-blocked at the Akamai/IP layer from this
> machine. See [`banks/HDFC_Bank/notes.md`](banks/HDFC_Bank/notes.md) for
> the full investigation.

### What "success" looks like (real output, HDFC)

You should see logs like:

```
INFO bank_kb.registry: Loaded adapter for HDFC_Bank (hooks: render_page)
INFO bank_kb.orchestrator: [HDFC_Bank] starting (backfill mode)
INFO bank_kb.orchestrator: [HDFC_Bank] doc_type allowlist for this run: ['financial_result', 'investor_presentation', 'press_release']
INFO bank_kb.orchestrator: [HDFC_Bank] fetching IR index https://www.hdfc.bank.in/about-us/investor-relations
INFO bank_kb._adapters.hdfc_bank: HDFC CDN probe: FY24 Q1 IP found at https://www.hdfc.bank.in/.../q1fy24-earnings-presentation.pdf
INFO bank_kb._adapters.hdfc_bank: HDFC CDN probe: FY24 Q2 IP found at https://www.hdfc.bank.in/.../q2fy24-earnings-presentation.pdf
INFO bank_kb._adapters.hdfc_bank: HDFC CDN probe: FY24 Q3 IP found at https://www.hdfc.bank.in/.../q3fy24-earnings-presentation.pdf
INFO bank_kb._adapters.hdfc_bank: HDFC CDN probe: FY24 Q4 IP found at https://www.hdfc.bank.in/.../q4fy24-earnings-presentation.pdf
INFO bank_kb._adapters.hdfc_bank: HDFC: CDN probe added 4 IP anchor(s) not linked from the current IR page
INFO bank_kb._adapters.hdfc_bank: HDFC: FY26 -> 8 kept anchor(s)
INFO bank_kb._adapters.hdfc_bank: HDFC: FY25 -> 8 kept anchor(s)
INFO bank_kb._adapters.hdfc_bank: HDFC: FY24 -> 8 kept anchor(s)
INFO bank_kb.orchestrator: [HDFC_Bank] adapter render_page() returned 3 year-view(s) for https://www.hdfc.bank.in/about-us/investor-relations
INFO bank_kb.orchestrator: [HDFC_Bank] adapter authoritative: 24 link(s) across 3 year-view(s) (static HTML had 68, ignored) on https://...
INFO bank_kb.orchestrator: [HDFC_Bank] downloading https://www.hdfc.bank.in/...q4fy26-earnings-presentation.pdf -> HDFC_Bank/investor_presentations/FY2026/...
...
INFO bank_kb.orchestrator: [HDFC_Bank] done: discovered=24 (nse=0 ir=24) in_window=24 new=24 skipped=0 skipped_by_type=0 dl_fail=0 ex_fail=0 js_render=1
```

The three key lines:

- **`HDFC CDN probe: FY24 Q1 IP found at ...`** — the speculative probe
  found PDFs that are NOT linked from the live IR page. Up to 8 HEAD
  requests per missing (year, quarter); the probe terminates early on
  each hit. Harmless on years where nothing exists (FY22/FY23: all 404s).
- **`adapter authoritative: 24 link(s) ... (static HTML had 68, ignored)`** —
  proves the adapter's filter is in force; the engine ignored the 68
  un-filtered anchors that raw-HTML scraping would otherwise have used.
- **`nse=0 ir=24`** — NSE was skipped (per HDFC's `use_nse: false`), so the
  IR page (+ CDN probe) is the sole source.

After this run, `./_scheduler/refresh.sh --status` will show 24 HDFC_Bank
documents indexed (12 IPs + 12 PRs across FY24/FY25/FY26). A search like
`python3 query.py "net interest margin" --bank HDFC_Bank` should return
hits across HDFC's earnings decks for all three years.

### Force a clean fresh pilot for HDFC

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"

rm -rf ../HDFC_Bank                         # wipe downloaded PDFs + extracted text
sqlite3 kb_index.sqlite "DELETE FROM manifest WHERE bank = 'HDFC_Bank';
                         DELETE FROM documents WHERE bank = 'HDFC_Bank';
                         DELETE FROM doc_fts WHERE rowid NOT IN (SELECT id FROM documents);"
./_scheduler/refresh.sh --mode backfill --force   # re-fetch from scratch
```

If `new_downloads=0` and `discovered=0`, see the
[Troubleshooting](#troubleshooting) section — usually a network/proxy issue.

---

## Verifying which config the engine loaded

When you run for HDFC (or any bank), every config decision shows up in the
verbose log. `./_scheduler/refresh.sh` already runs verbose by default, so
you can grep the output for these lines:

| Log line | What it proves |
| --- | --- |
| `Loaded adapter for HDFC_Bank (hooks: render_page)` | The per-bank `adapter.py` was discovered and imported. `hooks:` lists the functions the adapter overrides (an absent line means "no custom adapter, generic path"). |
| `[HDFC_Bank] doc_type allowlist for this run: ['financial_result', 'investor_presentation', 'press_release']` | The whitelist from `settings.json` (and any `doc_types` override in the bank's `config.json`) — anything outside this list is dropped at classify time. |
| `[HDFC_Bank] adapter render_page() returned 3 year-view(s)` | The adapter ran and returned blobs. If you wrote a new adapter and don't see this, your `render_page()` returned `None`/`[]` and the generic renderer took over. |
| `[HDFC_Bank] adapter authoritative: 24 link(s) ... (static HTML had 68, ignored)` | The orchestrator is honoring the adapter's filter — it discarded the raw-HTML scrape (which would re-inject the rows the adapter filtered out). |
| `discovered=24 (nse=0 ir=24)` | NSE contribution for this bank. `nse=0` proves `use_nse: false` is honored; a non-zero number means NSE was queried and returned filings. |
| `new=N skipped=M` | `skipped=M` means the `manifest` table deduplicated `M` already-downloaded URLs — re-runs are idempotent. |

Run it and watch the relevant lines:

```bash
./_scheduler/refresh.sh focus HDFC_Bank
./_scheduler/refresh.sh --mode daily 2>&1 | \
    grep -E '(Loaded adapter|allowlist|adapter render_page|adapter authoritative|discovered=|TOTALS)'
```

If you want to inspect the loaded config **without firing the network**, the
registry exposes everything in one call:

```bash
.venv/bin/python3 - <<'PY'
from pathlib import Path
from bank_kb.registry import (
    load_settings, load_all_banks,
    adapter_render_page, adapter_classify_link, _adapter_hooks,
)
import json

ENGINE = Path(".").resolve()
print("settings.json:")
s = load_settings(ENGINE)
for k in ("history_years","request_delay_seconds","request_timeout_seconds",
          "max_pdf_size_mb","doc_types_whitelist"):
    print(f"  {k:28s} = {s[k]!r}")

hdfc = load_all_banks(ENGINE, only="HDFC_Bank")[0]
print(f"\nbanks/HDFC_Bank/:")
print(f"  workspace folder           = {hdfc.folder}")
print(f"  adapter loaded?            = {hdfc.adapter is not None}")
print(f"    hooks defined            = {sorted(_adapter_hooks(hdfc.adapter)) or '<none>'}")
print(f"    render_page wired?       = {adapter_render_page(hdfc) is not None}")
print(f"    classify_link wired?     = {adapter_classify_link(hdfc) is not None}")
print(f"  effective use_nse          = {hdfc.config.get('use_nse', True)}")
print(f"\n  config.json (after comment-stripping + defaults):")
print(json.dumps(hdfc.config, indent=4))
PY
```

Expected output for HDFC today:

```
settings.json:
  history_years                = 5
  doc_types_whitelist          = ['investor_presentation', 'press_release', 'financial_result']
banks/HDFC_Bank/:
  adapter loaded?            = True
    hooks defined            = ['render_page']
    render_page wired?       = True
  effective use_nse          = False
  config.json:
    { "name": "HDFC_Bank", "ticker": "HDFCBANK", "use_nse": false,
      "sources": [{ "url": "https://www.hdfc.bank.in/about-us/investor-relations",
                    "type_hint": "mixed", "requires_js": true }] }
```

Any drift here (e.g. `adapter loaded? = False`, or `use_nse = True`) tells
you the file isn't where the engine is looking for it, or you've edited the
wrong copy.

---

## Refreshing the KB

Two ways to refresh, both fully wired up:

- **Automatic** — a macOS LaunchAgent fires `run_daily.sh` hourly between
  10:00 and 15:00 local time, every day. The first fire that succeeds writes
  a per-day flag (`_logs/.success_YYYY-MM-DD.flag`); subsequent fires that
  day detect the flag and exit in ~30 ms. Failures are retried by the next
  hour's fire. After 15:00 with no success, the day is over and the next
  attempt is tomorrow at 10:00. See [Daily auto-schedule](#daily-auto-schedule)
  to install / verify / uninstall.
- **Manual** — `./_scheduler/refresh.sh` from Cursor's terminal. Foreground,
  prints live output, independent of the LaunchAgent. Use this for adding a
  new bank, debugging discovery on one URL, or running outside the 10–15
  window. Manual runs don't touch the per-day success flag (they don't make
  the next scheduled fire skip).

![Architecture](architecture.svg)

```bash
cd /Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler

# Focus mechanism (persistent — survives shell restart via .kb_focus)
./refresh.sh focus HDFC_Bank                  # set the active bank
./refresh.sh focus                            # show current focus
./refresh.sh unfocus                          # clear focus

# Bank scope
./refresh.sh                                  # daily, focused bank (or ALL if no focus)
./refresh.sh --bank Kotak_Mahindra_Bank       # explicit override of focus
./refresh.sh --mode backfill --bank Yes_Bank  # 5-yr history for one bank
./refresh.sh --all-banks --mode daily         # force every registered bank (ignoring focus)
KB_FOCUS=Axis_Bank ./refresh.sh               # env var overrides .kb_focus for one shell

# Document-type scope (pick one or many — folder names you see on disk)
./refresh.sh --type press_releases            # PRs only, focused bank
./refresh.sh --bank HDFC_Bank --type investor_presentations
./refresh.sh --mode backfill --bank ICICI_Bank --type press_releases
./refresh.sh --type investor_presentations,press_releases    # both, comma form

# Pin the source URL(s) instead of using banks/<X>/config.json sources.
# Requires --bank (or a focus). NSE discovery is skipped automatically.
./refresh.sh --bank Kotak_Mahindra_Bank \
    --url https://www.kotak.bank.in/en/investor-relations/financial-results.html
./refresh.sh --mode backfill --bank IDFC_First_Bank --type press_releases \
    --url https://www.idfcfirst.bank.in/our-history/news-media/press-releases
./refresh.sh --bank Yes_Bank --url URL_1 --url URL_2         # multiple URLs
./refresh.sh --bank Yes_Bank --url URL_1,URL_2               # same, comma form

# Utilities (no fetch)
./refresh.sh --status                         # focus + corpus stats
./refresh.sh --sync-structure                 # create folders for any registered bank
```

**When to use `--url`** — testing a candidate URL before you commit it to
`banks/<Bank>/config.json`, pulling from a one-off archive page, or pinning
to a single source while you debug discovery on that page. The downloaded
PDFs still land in the bank's normal folders (`<Bank>/<type>/FY<N>/`) and
the classifier still runs — `--url` only changes *where* the engine looks
for PDF anchors. If exactly one `--type` is also passed, it's used as the
`type_hint` for the synthetic source to help the classifier disambiguate
ambiguous URLs.

The three document categories you can filter on (anything outside this set
is dropped at classify time and never downloaded):

| `--type` (folder name) | Singular alias | What goes in here |
| --- | --- | --- |
| `investor_presentations` | `investor_presentation`, `presentations`, `ip` | Quarterly investor decks, analyst presentations |
| `financial_results`      | `financial_result`, `fr`                       | Quarterly result PDFs (stored alongside IPs on disk) |
| `press_releases`         | `press_release`, `pr`                          | Quarterly result PRs + newsroom announcements |

To expand or restrict the engine-wide set, edit `settings.json:doc_types_whitelist`.
To narrow it for one bank only, set `doc_types: [...]` in that bank's
`banks/<Bank>/config.json`.

What `refresh.sh` does for you that running `python3 run.py` directly doesn't:

1. **Picks the right Python.** Activates `_engine/.venv` automatically — no
   `ModuleNotFoundError: requests` from stray system pythons.
2. **Preflights deps.** Refuses to start if `requests`/`bs4` aren't importable,
   prints the exact `pip install -r requirements.txt` command.
3. **Refuses to race other engine instances.** If something already has a lock
   on `kb_index.sqlite`, it bails out (override with `--force`).
4. **Prints a Cowork-style summary at the end.** Per-bank breakdown of new
   downloads, NSE noise separated from real errors, link to the JSON log.

Set an alias for daily use:

```bash
alias kb='/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler/refresh.sh'
# kb                         # daily, all banks
# kb --bank Kotak_Mahindra_Bank
# kb --status
```

### Daily auto-schedule

The macOS LaunchAgent is the unattended path. Schedule is fixed business
policy (not a knob): **6 fires per day at 10:00 / 11:00 / 12:00 / 13:00 /
14:00 / 15:00 local time, every day**, with first-success-wins so only one
fire per day actually runs the engine.

| When | What happens |
| --- | --- |
| 10:00 | Wrapper fires. If `_logs/.success_$(today).flag` doesn't exist, it runs the engine. On exit 0, it writes the flag. |
| 11:00–15:00 | Wrapper fires. If the flag exists, it logs "today already succeeded — no work to do" and exits 0 in ~30 ms. Otherwise it retries. |
| After 15:00 with no success | Day is over. The flag isn't written. Next attempt is tomorrow at 10:00. |
| Mac was asleep at fire time | launchd fires the missed event on next wake. Flag protects against double-running on the same day. |

Install / verify / uninstall:

```bash
cd /Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_scheduler

./install.sh                    # auto-detects python (prefers .venv with Playwright)
./install.sh --python /opt/homebrew/bin/python3   # explicit python

./status.sh                     # loaded? next fire? today's flag state?
./uninstall.sh                  # remove the agent
```

Trigger / force / inspect:

```bash
# Trigger NOW, honoring today's flag (skip if already succeeded today)
launchctl start com.rahul.banking-kb-daily-refresh
# or:  ./run_daily.sh

# Force a re-run within the day (ignores today's flag)
FORCE=1 ./run_daily.sh
# or:  rm "../_logs/.success_$(date +%Y-%m-%d).flag" && ./run_daily.sh

# Tail the latest wrapper log
ls -1t ../_logs/wrapper_*.log | head -1 | xargs tail -30
```

### Optional: Cowork chat report

`banking-kb-daily-refresh` Cowork task is `enabled: false` by default.
Toggle on from Cowork's Scheduled sidebar (or via
`mcp__scheduled-tasks__update_scheduled_task` with `enabled: true`) if you
want a daily chat summary of the freshest engine run log. The task is
read-only — it doesn't run the engine itself, just summarizes
`_engine/_logs/run_*.json`.

### Status check

Two complementary status commands:

```bash
./_scheduler/refresh.sh --status   # engine: focus, last-run time, corpus stats, doc-type whitelist
./_scheduler/status.sh             # scheduler: loaded? next fire? today's success flag?
```

`refresh.sh --status` answers "what's in the index right now?" — focus,
last-run time, age of last run, total documents indexed, breakdown by bank
and document type. `status.sh` answers "is the LaunchAgent doing its job?"
— whether the agent is loaded, when it next fires, whether today's run
has already succeeded.

If `refresh.sh --status` shows the last run was several days ago and
`status.sh` says the agent is loaded, see [Troubleshooting](#troubleshooting)
— usually a permissions issue (Full Disk Access) silently blocking agent
fires.

---

## Searching the corpus

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"
source .venv/bin/activate

# Top investor-presentation hits across all banks for "generative AI":
python3 query.py "generative AI" --type investor_presentation

# Hyphens, slashes, etc. work — they're auto-quoted as phrases:
python3 query.py "co-lending OR account aggregator"

# HDFC-only, last FY:
python3 query.py "digital banking" --bank HDFC_Bank --fy 2025

# Everything tagged with the digital_banking topic (no keyword needed):
python3 query.py "" --topic digital_banking --limit 50

# Raw FTS5 query (boolean operators, NEAR, column matches, etc.):
python3 query.py "(AI OR genai) AND NOT KYC" --raw

# Corpus stats only:
python3 query.py --stats

# JSON output (best for piping into Claude/an LLM as context):
python3 query.py "core banking modernization" --json > /tmp/ctx.json
```

### Topic flags

Each PDF is auto-tagged with any of these topics that appear in its text. Use
`--topic` to filter:

| Topic | Matches phrases like |
|---|---|
| `ai_ml` | generative AI, LLM, chatbot, ML model |
| `digital_banking` | digital channel, mobile banking, video KYC |
| `channels_integration` | omnichannel, API banking, BaaS, account aggregator |
| `core_banking` | Finacle, FlexCube, T24, core modernization |
| `cloud_infra` | AWS, Azure, GCP, cloud migration |
| `data_analytics` | data lake, customer 360, real-time analytics |
| `cyber_security` | fraud detection, zero trust, infosec |
| `payments` | UPI, CBDC, cards, merchant acquiring |
| `retail_journeys` | customer journey, CX, hyper-personalization |
| `msme_corporate` | MSME, supply chain finance, working capital |
| `wealth` | HNI, AUM, private banking |
| `esg_sustainability` | ESG, green finance, climate |

### CXO-prep workflow

```bash
# Pull HDFC's recent investor-deck text on AI / digital:
python3 query.py "AI OR generative OR digital" --bank HDFC_Bank \
        --type investor_presentation --limit 8 --json > /tmp/hdfc_ctx.json
```

Then paste that JSON into Claude with: *"Here's HDFC's own language on AI and
digital strategy from their last few investor decks. Draft a 3-slide CXO
narrative that maps our tech stack to the priorities they've called out."*

---

## Adding a new bank or customer

`add_bank.py` does two things:

1. **Scaffolds the bank's workspace** at `banks/<Name>/` by copying from
   `banks/_template/` — gives you `config.json` (the bank's ticker, IR
   sources, requires_js flag) and `notes.md` (the troubleshooting log you
   grow as you learn the bank's quirks). Optionally also `adapter.py` for
   custom render/classify hooks.
2. **Immediately runs the 5-year backfill** for that one bank — NSE
   corporate-announcements + IR-page scrape + Playwright (if needed) →
   download → extract → index. Pass `--no-backfill` to skip.

The corpus folders (`<Bank>/{investor_presentations,press_releases,extracted_text}/`)
are also created. The list of per-bank subfolders lives in
`bank_kb/structure.py` as `SUBFOLDERS` — change it there once and both
`add_bank.py` and `run.py --sync-structure` start creating the new layout.

### Add a new NSE-listed bank (and pull 5 years of data)

```bash
python3 add_bank.py South_Indian_Bank \
    --ticker SOUTHBANK --category private \
    --source 'https://www.southindianbank.com/investor-relations' investor_presentation \
    --source 'https://www.southindianbank.com/financial-results' financial_result
```

What happens after you press Enter:

1. `banks/South_Indian_Bank/{config.json,notes.md}` scaffolded from the template.
2. `South_Indian_Bank/{investor_presentations,press_releases,extracted_text}/` corpus folders created.
3. NSE warmed and queried for `SOUTHBANK` filings (last 5 years).
4. Both IR pages fetched (with Playwright if `--requires-js`) and PDF anchors harvested.
5. Every new PDF downloaded to the right `FY<N>/` subfolder, extracted, and indexed.
6. Run summary saved to `_engine/_logs/run_YYYY-MM-DD_HHMM_addbank_South_Indian_Bank.json`.
7. Summary printed: `discovered=N (nse=N, ir=N), downloaded=N, skipped_by_type=N, failures=...`.

### Add a non-bank customer (NBFC, insurance, fintech)

```bash
python3 add_bank.py Bajaj_Finance --category nbfc \
    --source 'https://www.bajajfinserv.in/investor-relations'
```

No NSE ticker means the engine relies on IR-page scraping alone — perfectly
fine for unlisted entities or non-bank customers.

### Add a customer with a JS-rendered IR page

```bash
python3 add_bank.py HDFC_Life \
    --ticker HDFCLIFE --category insurance \
    --source 'https://www.hdfclife.com/investor-relations/financials' \
    --requires-js
```

`--requires-js` tells the engine to render this source in headless Chromium
via Playwright before scraping links — needed for client-rendered IR pages.

### Bank with quirky JS (write a per-bank adapter)

If the generic Playwright path can't drive the bank's UI (custom React
year-picker, "Load more" buttons, hover menus, aria-label-only doc-type
signals), write a per-bank `adapter.py`. The HDFC_Bank adapter shipped in
this repo is the worked example — read it end-to-end before writing a new
one:

- [`banks/HDFC_Bank/adapter.py`](banks/HDFC_Bank/adapter.py) — ~150 lines.
  Loads HDFC's IR page once, filters anchors via `aria-label`, parses fiscal
  period from URL path, returns synthetic HTML per year.
- [`banks/HDFC_Bank/notes.md`](banks/HDFC_Bank/notes.md) — narrates exactly
  why the generic engine path is wrong for HDFC and what the adapter does
  about it. Read this first if you're about to clone the approach.
- [`banks/HDFC_Bank/config.json`](banks/HDFC_Bank/config.json) — shows the
  three flags that matter: `requires_js: true` (route through JS path),
  `use_nse: false` (skip NSE — the adapter is the authoritative source),
  no per-bank `doc_types` (inherit the global IP/PR/FR whitelist).

Workflow for a new bank that needs an adapter:

```bash
# 1. Scaffold the workspace (no backfill yet — adapter doesn't exist).
python3 add_bank.py Some_Bank --ticker SOMEBANK --category private \
    --source 'https://www.somebank.com/investor-relations' \
    --requires-js --no-backfill

# 2. Copy the adapter stub and crib HDFC's structure for your starting point.
cp banks/_template/adapter.py.example banks/Some_Bank/adapter.py
# (or:  cp banks/HDFC_Bank/adapter.py banks/Some_Bank/adapter.py  and edit)

# 3. Implement render_page() — see banks/README.md for the contract.
#    Test it in isolation first; the script at _scratch/probe_hdfc.py is a
#    good template for one-off page-structure investigation.

# 4. Once the adapter returns the right blobs, backfill the bank.
./_scheduler/refresh.sh focus Some_Bank
./_scheduler/refresh.sh --mode backfill --force
```

The engine auto-loads `banks/<Bank>/adapter.py` on every run and routes the
bank's IR-page scraping through `adapter.render_page` (falling back to the
generic renderer if the adapter returns `None`/`[]`). When the adapter does
return blobs they are **authoritative** — static-HTML scrape is ignored
even if it found more links, because the adapter's whole reason to exist
is to filter the page down. See [`banks/README.md`](banks/README.md) for the
full adapter contract.

### Register without fetching (rare)

```bash
python3 add_bank.py X_Bank \
    --ticker XBANK --category private \
    --source 'https://www.x.com/ir' --no-backfill
```

When you're ready to fetch later:

```bash
python3 run.py --mode backfill --bank X_Bank --verbose
```

### Merge new sources into an existing bank

Re-running `add_bank.py` with the same name merges new `--source` entries
into the existing `banks/<Bank>/config.json` without duplicating URLs.
The backfill that follows is idempotent — only new (unseen) URLs get
downloaded. Use `--replace` if you want to wipe the bank's workspace and
start over from the template.

### Remove a bank

```bash
rm -rf banks/HDFC_Bank          # forget config + notes + adapter
rm -rf ../HDFC_Bank             # forget downloaded PDFs + extracted text
sqlite3 kb_index.sqlite "DELETE FROM manifest WHERE bank='HDFC_Bank';
                         DELETE FROM documents WHERE bank='HDFC_Bank';
                         DELETE FROM doc_fts WHERE rowid NOT IN (SELECT id FROM documents);"
```

---

## How auto-discovery works

For each bank, every run hits **three** discovery sources and merges results:

1. **NSE corporate-announcements API** — the primary, hands-off source. Every
   NSE-listed bank in scope is required by SEBI to file investor
   presentations, annual reports, earnings transcripts, and financial results
   on NSE. The engine warms up NSE cookies once per run, then asks the API for
   each ticker's filings over the last `history_years` window. NSE returns
   the authoritative filing date for every PDF, which the engine uses to
   filter by the history window even when filenames have no date in them.
   Annual reports / transcripts are then dropped at classify time per the
   doc-type whitelist — only IP / PR / financial_result survive.

   You can **disable NSE for an individual bank** with `"use_nse": false`
   in `banks/<Bank>/config.json` — useful when the bank's own IR page is
   curated, exhaustive, and indexed by a custom adapter (HDFC is the
   reference case — NSE for HDFC adds ~50 duplicate / off-topic filings
   that bypass the adapter's filter). Default is `true`.

2. **Bank IR page HTML scraping** — for each `source` URL in
   `banks/<Bank>/config.json`, the engine fetches the page and harvests every
   `<a href="*.pdf">` anchor. This catches the press releases, special updates,
   and additional decks that banks publish on their own site but don't file
   formally with NSE.

3. **Playwright headless render (auto-fallback + multi-year iteration)** —
   when a `source` is marked `requires_js: true` (HDFC, ICICI, SBI, Axis),
   or when raw-HTML scraping returns zero links, the engine re-renders the
   page in headless Chromium and parses the resolved HTML. If the page
   exposes a native `<select>` year dropdown (Axis investor presentations),
   the renderer iterates every option inside the history window so the full
   5-year backfill happens in one pass — no manual per-year URLs required.
   For banks with custom non-native pickers, a per-bank `adapter.py` with
   a `render_page()` hook takes over.

All three sources flow into the same dedup-by-URL stream, so there's no
double-download even when a doc appears on multiple sources. The `manifest`
table makes re-runs idempotent.

There's still a `seed_urls` escape hatch (an optional list in
`banks/<Bank>/config.json`) for unusual one-off cases — an old historical
PDF you want to backfill from a specific URL — but you never need to touch
it for routine new-quarter ingestion.

---

## Command reference (cheat sheet)

All commands assume you're inside the engine directory with the virtualenv
activated:

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"
source .venv/bin/activate
```

| What you want | Command |
|---|---|
| Add a brand-new bank + immediately backfill 5y | `python3 add_bank.py HDFC_Bank --ticker HDFCBANK --category private --source URL --requires-js` |
| Scaffold the bank without fetching | `python3 add_bank.py X_Bank --ticker X --category private --source URL --no-backfill` |
| Set the focused bank (persistent) | `./_scheduler/refresh.sh focus HDFC_Bank` |
| Show / clear focus | `./_scheduler/refresh.sh focus` &nbsp;/&nbsp; `./_scheduler/refresh.sh unfocus` |
| Per-shell focus override (env var) | `KB_FOCUS=Axis_Bank ./_scheduler/refresh.sh --mode daily` |
| Backfill the focused bank | `./_scheduler/refresh.sh --mode backfill` |
| Daily refresh, focused bank | `./_scheduler/refresh.sh --mode daily`  (or just `./_scheduler/refresh.sh`) |
| Force every registered bank (ignore focus) | `./_scheduler/refresh.sh --all-banks --mode daily` |
| Single bank, explicit (overrides focus) | `./_scheduler/refresh.sh --mode backfill --bank ICICI_Bank` |
| See focus + corpus stats | `./_scheduler/refresh.sh --status` |
| Heal corpus folders for all registered banks | `./_scheduler/refresh.sh --sync-structure` |
| Pin a URL for testing | `./_scheduler/refresh.sh --bank Yes_Bank --url https://...` |
| Search the corpus | `python3 query.py "your terms"` |
| Search with filters | `python3 query.py "AI" --bank HDFC_Bank --type investor_presentation --fy 2025` |
| Filter by topic only | `python3 query.py "" --topic digital_banking --limit 50` |
| Get JSON for piping to an LLM | `python3 query.py "..." --json` |
| Corpus stats | `python3 query.py --stats` |
| Inspect what config the engine loaded for a bank | see [Verifying which config the engine loaded](#verifying-which-config-the-engine-loaded) |
| Scheduler status (next fire, today's flag) | `./_scheduler/status.sh` |
| Install / uninstall the daily scheduler | `./_scheduler/install.sh` &nbsp;/&nbsp; `./_scheduler/uninstall.sh` |
| Trigger the scheduled run NOW (honors today's flag) | `./_scheduler/run_daily.sh` |
| Force a scheduled re-run within the day (ignores today's flag) | `FORCE=1 ./_scheduler/run_daily.sh` |

---

## Troubleshooting

### "No module named bank_kb" or similar import errors
Make sure you're running from the `_engine/` directory and the virtualenv is
activated:
```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"
source .venv/bin/activate
```

### Playwright errors / no JS-render happening
Confirm Playwright is installed and its Chromium is downloaded:
```bash
python3 -m pip show playwright
python3 -m playwright install chromium
```
The engine logs `Playwright unavailable: ...` if it can't import — Playwright
is optional; the engine still uses NSE for the rest.

### A specific bank shows `discovered_via_nse=0` in the run log
Either:
- The ticker symbol in `banks/<Bank>/config.json` is wrong → fix it and re-run.
- NSE rate-limited or blocked the warmup → just re-run a few minutes later.
- The bank isn't NSE-listed → that's expected, IR scraping still runs.

### A specific bank shows `discovered_via_ir=0` in the run log
- Open the source URL in a browser. If it requires login or is gated, scraping
  won't work — remove the source and rely on NSE.
- If the page renders fine in a browser but the engine sees nothing, add
  `"requires_js": true` to that source in `banks/<Bank>/config.json` and re-run.
- If `requires_js` is already set and you still get zero links, the bank's
  page probably uses a non-native JS widget the generic renderer can't drive
  — that's when you write a per-bank `adapter.py` with a `render_page()`
  hook (see [`banks/README.md`](banks/README.md)).

### Last run was days ago — what should I do?
```bash
./_scheduler/refresh.sh --status                # see how stale
./_scheduler/refresh.sh --mode daily            # manual catch-up
./_scheduler/status.sh                          # if the LaunchAgent is installed, check why it didn't fire
```
The most common cause when the agent is installed but not firing is missing
**Full Disk Access** for `/bin/bash` and the Python binary — see the
[scheduler README](_scheduler/README.md#macos-specifics-worth-knowing).

### Search returns nothing for a term you know exists in a downloaded PDF
- Confirm the PDF was indexed:
  ```bash
  python3 query.py --stats
  ```
- Inspect the extracted text directly:
  ```bash
  grep -l "your phrase" /Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/*/extracted_text/*.txt
  ```
- If the term is hyphenated or has unusual chars, the auto-quoter handles
  that, but you can also pass `--raw` for explicit FTS5 syntax.

### Force a fresh rebuild for one bank
```bash
# Remove the per-bank manifest entries:
sqlite3 kb_index.sqlite "DELETE FROM manifest WHERE bank = 'HDFC_Bank';"
sqlite3 kb_index.sqlite "DELETE FROM documents WHERE bank = 'HDFC_Bank';"
sqlite3 kb_index.sqlite "DELETE FROM doc_fts WHERE rowid NOT IN (SELECT id FROM documents);"
# Then re-backfill:
python3 run.py --mode backfill --bank HDFC_Bank --verbose
```

### Find run logs
```bash
ls -lt "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine/_logs/" | head
```
Each `run_YYYY-MM-DD_HHMM.json` has per-bank counts, errors, and the index
totals at end-of-run. `wrapper_YYYY-MM-DD_HHMM.log` files in the same folder
are the LaunchAgent's stdout/stderr capture — useful when the engine didn't
even start (e.g. wrong Python path) and so no JSON log was written.

---

## Files & data hygiene

- The engine never modifies bank servers (read-only HTTP GETs).
- Every downloaded URL is recorded in `manifest` so re-runs are idempotent.
- Run summaries land in `_engine/_logs/run_YYYY-MM-DD_HHMM.json` for audit/debugging.
- Delete `kb_index.sqlite` and any bank folder if you want to force a fresh
  rebuild for that bank.
- The 5-year history window is in `settings.json` (`history_years`).
- The default `request_delay_seconds` is 2 — increase if banks rate-limit
  you, decrease if you're on a fast pipe and want a faster run.
- `.kb_focus` is per-machine state — it's gitignored and is never shared
  across checkouts. Same for `KB_FOCUS` env var.
- Per-bank state lives entirely under `banks/<Bank>/` (committed) and
  `KnowledgeBase/<Bank>/` (not committed — large blobs). To completely
  forget a bank: delete both folders + clean its rows from `kb_index.sqlite`
  (see [Remove a bank](#remove-a-bank)).
