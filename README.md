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

Each bank lives in its own folder under `banks/<BankName>/`:

```
banks/HDFC_Bank/
├── config.json    ← ticker, IR sources, requires_js flag, optional per-bank doc_types
├── notes.md       ← human troubleshooting log ("HDFC's React picker breaks because…")
└── adapter.py     ← OPTIONAL — custom Python to drive bank-specific UI quirks
```

See [`banks/README.md`](banks/README.md) for the full per-bank file shape and
adapter examples. The starting state ships with zero registered banks; add
them one at a time with `python3 add_bank.py <Name>` as you onboard each.

---

## Contents

1. [Layout on disk](#layout-on-disk)
2. [First-time setup](#first-time-setup)
3. [End-to-end pilot on one bank](#end-to-end-pilot-on-one-bank)
4. [Backfill (run once)](#backfill-run-once)
5. [Daily refresh — automatic and manual](#daily-refresh--automatic-and-manual)
6. [Searching the corpus](#searching-the-corpus)
7. [Adding a new bank or customer](#adding-a-new-bank-or-customer)
8. [How auto-discovery works](#how-auto-discovery-works)
9. [Command reference (cheat sheet)](#command-reference-cheat-sheet)
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
│   ├── _scheduler/          # the manual refresh.sh + opt-in LaunchAgent + Cowork prompt
│   │   ├── refresh.sh                                  # ← THE entry point. Includes `focus` / `unfocus` subcommands.
│   │   ├── run_daily.sh                                # wrapper launchd would invoke (LaunchAgent is opt-in)
│   │   ├── com.rahul.banking-kb-daily-refresh.plist.template
│   │   ├── install.sh / uninstall.sh / status.sh       # opt-in LaunchAgent lifecycle (off by default)
│   │   ├── cowork_report_prompt.md                     # report prompt (Cowork task is disabled by default)
│   │   └── README.md
│   ├── kb_index.sqlite      # FTS5 search index (created on first run, gitignored)
│   ├── requirements.txt
│   ├── run.py               # backfill/daily orchestrator + --status + --sync-structure
│   ├── add_bank.py          # onboard new banks/customers (scaffolds banks/<Name>/)
│   ├── query.py             # CLI search
│   ├── ALLOWLIST_REQUIRED.txt  # only relevant if you ever run fetch inside Cowork
│   └── README.md            # this file
├── HDFC_Bank/                                          # ← bank corpus folders (siblings of _engine/)
│   ├── investor_presentations/FY2026/Q2FY26-Earnings-Presentation.pdf
│   ├── press_releases/FY2026/...
│   └── extracted_text/*.txt
├── ICICI_Bank/
│   └── ...
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

The engine ships with **zero registered banks**. You add them one at a time
as you onboard each — see [Adding a new bank](#adding-a-new-bank-or-customer).
The full first-bank flow is:

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"
source .venv/bin/activate

# 1. Scaffold the bank's workspace and immediately backfill 5 years.
#    Axis Bank is a good first pilot — server-rendered IR pages (Playwright
#    not strictly required) and an NSE ticker (so NSE auto-discovery also
#    contributes).
python3 add_bank.py Axis_Bank \
    --ticker AXISBANK --category private \
    --source 'https://www.axisbank.com/shareholders-corner/other-information/investor-presentations' investor_presentation \
    --source 'https://www.axisbank.com/shareholders-corner/financial-information-and-others/press-releases' press_release \
    --requires-js --no-edit

# 2. Confirm docs landed and are indexed.
python3 run.py --status

# 3. Sanity-check searchability.
python3 query.py "digital banking OR generative AI" --bank Axis_Bank --limit 5
python3 query.py "" --topic ai_ml --bank Axis_Bank --limit 5

# 4. Run --mode daily to confirm dedup works (should show new_downloads=0).
./_scheduler/refresh.sh focus Axis_Bank
./_scheduler/refresh.sh --mode daily
```

What that does end-to-end:

1. Creates `banks/Axis_Bank/{config.json, notes.md}` from the template.
2. Creates `../Axis_Bank/{investor_presentations, press_releases, extracted_text}/`.
3. Warms NSE cookies once and pulls Axis filings from the NSE
   corporate-announcements API (filtered to IP / PR / financial_result via
   `settings.json:doc_types_whitelist`).
4. Fetches both Axis IR landing pages with Playwright (`--requires-js`),
   iterates the native `<select>` year dropdown for the last 5 years, and
   harvests every `<a *.pdf>` anchor across all year-views.
5. Merges + dedups both source lists by URL.
6. Downloads each new PDF into `Axis_Bank/<type>/FY<N>/`.
7. Extracts text into `Axis_Bank/extracted_text/`.
8. Indexes everything into `kb_index.sqlite`.
9. Writes a run summary to `_engine/_logs/run_YYYY-MM-DD_HHMM_addbank_Axis_Bank.json`.

### Force a clean fresh pilot

```bash
cd "/Users/rahul.choubey/Documents/RWork/Banking_Data/KnowledgeBase/_engine"

# Wipe everything Axis-related: workspace, data folder, focus.
rm -rf banks/Axis_Bank
rm -rf ../Axis_Bank
[ -f .kb_focus ] && rm .kb_focus

# Drop Axis rows from the index (skip if you also want to keep other banks).
sqlite3 kb_index.sqlite "DELETE FROM manifest WHERE bank = 'Axis_Bank';
                         DELETE FROM documents WHERE bank = 'Axis_Bank';
                         DELETE FROM doc_fts WHERE rowid NOT IN (SELECT id FROM documents);"

# Re-onboard from scratch (see step 1 above).
```

### What "success" looks like

You should see logs like:

```
INFO bank_kb.orchestrator: [Axis_Bank] starting (backfill mode)
INFO bank_kb.orchestrator: [Axis_Bank] doc_type allowlist for this run: ['financial_result', 'investor_presentation', 'press_release']
INFO bank_kb.nse_source: NSE: 35 announcements for AXISBANK in last 5 years
INFO bank_kb.orchestrator: [Axis_Bank] fetching IR index https://www.axisbank.com/...
INFO bank_kb.js_fetcher: iterating <select> year-views: 5 in window
INFO bank_kb.orchestrator: [Axis_Bank] downloading https://...Q2FY26... -> Axis_Bank/investor_presentations/FY2026/...pdf
...
INFO bank_kb.orchestrator: [Axis_Bank] done: discovered=48 (nse=35 ir=13) in_window=45 new=45 skipped=0 skipped_by_type=11 dl_fail=0 ex_fail=0 js_render=1
```

`skipped_by_type=N` indicates how many discovered PDFs were dropped because
they classified as something OUTSIDE the doc-type whitelist (annual reports,
transcripts, footer noise) — that's the new tightening doing its job.

Then `python3 run.py --status` will show ~30–50 Axis_Bank documents indexed
across investor_presentation / press_release / financial_result. A test query
like `python3 query.py "digital" --bank Axis_Bank` should return hits with
snippets highlighting the matched terms.

If `new_downloads=0` and `discovered=0`, see the
[Troubleshooting](#troubleshooting) section — usually a network/proxy issue.

---

## Backfill / refresh, scoped by focus

You'll typically operate on **one bank at a time** — you set a focus, then
every subsequent command implicitly scopes to that bank. No `--bank` flag
repetition.

```bash
./_scheduler/refresh.sh focus HDFC_Bank          # set the active bank
./_scheduler/refresh.sh --mode backfill          # 5-year backfill for HDFC_Bank
./_scheduler/refresh.sh                          # daily, HDFC_Bank
./_scheduler/refresh.sh --type press_releases    # PRs only, HDFC_Bank
./_scheduler/refresh.sh focus                    # show current focus
./_scheduler/refresh.sh unfocus                  # clear focus
```

Override per-shell with the `KB_FOCUS` env var:

```bash
KB_FOCUS=Axis_Bank ./_scheduler/refresh.sh --mode daily
```

Override per-command with the explicit `--bank` flag (highest precedence):

```bash
./_scheduler/refresh.sh --mode backfill --bank ICICI_Bank
```

To run **every** registered bank in one go (ignoring focus), pass
`--all-banks`:

```bash
./_scheduler/refresh.sh --all-banks --mode daily
```

Expect per-bank backfill cost:

- 20–80 PDFs typical (varies by how many press releases the bank lists)
- 100–500 MB of disk
- 2–10 minutes on a residential link (the engine sleeps 2 s between
  requests to be polite — adjust `request_delay_seconds` in `settings.json`
  if you have a faster pipe)

The engine is **idempotent**. Re-running backfill is safe — already-downloaded
URLs are skipped via the `manifest` table.

---

## Refreshing the KB

The engine runs **only when you trigger it.** No timer, no daemon — both the
macOS LaunchAgent and the Cowork scheduled task are disabled. One script,
one entry point.

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

### Re-enabling the daily auto-schedule (if you ever want it back)

Two switches are off by default in this checkout:

- **macOS LaunchAgent** (the unattended 08:30 Mon-Fri run): turn back on
  with `./_engine/_scheduler/install.sh` (custom time via `--time HH:MM`).
  Uninstall with `./_engine/_scheduler/uninstall.sh`.
- **Cowork scheduled task** `banking-kb-daily-refresh`: currently
  `enabled: false`. Toggle it back on from Cowork's Scheduled sidebar
  (or via `mcp__scheduled-tasks__update_scheduled_task` with `enabled: true`).
  The task only reads `_engine/_logs/*.json` — it doesn't run the engine
  itself, so re-enabling it without re-installing the LaunchAgent will
  start producing `STALE` reports whenever you haven't run `refresh.sh`
  recently. That's by design.

The two switches are independent. Re-enable just one if you want chat
reports of your manual runs without an unattended schedule, or just the
other for unattended fetches with no chat reporting.

### Status check

```bash
./_engine/_scheduler/refresh.sh --status
```

Shows last-run time, age of last run, total documents indexed, and the
breakdown by bank and document type. If the last run is older than a few
days, that's the cue to run `./refresh.sh` (without `--status`) to catch up.

### Single-bank refresh

```bash
./_engine/_scheduler/refresh.sh --bank ICICI_Bank
./_engine/_scheduler/refresh.sh --mode backfill --bank Yes_Bank
```

You can still call `python3 _engine/run.py` directly if you prefer — it's
what `refresh.sh` invokes under the hood — but the wrapper handles the
venv activation, dep check, lock collision, and summary printing for you.

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
year-picker, "Load more" buttons, hover menus), add a custom adapter:

```bash
# 1. Scaffold + manually open notes.md in your editor (no backfill yet)
python3 add_bank.py HDFC_Bank --ticker HDFCBANK --category private \
    --source 'https://www.hdfcbank.com/personal/about-us/investor-relations' \
    --requires-js --no-backfill

# 2. Copy the adapter stub and start customizing
cp banks/_template/adapter.py.example banks/HDFC_Bank/adapter.py

# 3. Edit banks/HDFC_Bank/adapter.py:
#    Implement render_page() to click HDFC's custom dropdown and return
#    list[(year_label, html_bytes)]. See banks/README.md for the contract.

# 4. Once the adapter works, backfill
python3 run.py --mode backfill --bank HDFC_Bank --verbose
```

The engine auto-loads `banks/<Bank>/adapter.py` on every run and routes the
bank's IR-page scraping through `adapter.render_page` (falling back to the
generic renderer if the adapter returns `None`/`[]`). See
[`banks/README.md`](banks/README.md) for the full adapter contract.

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
| List scheduled tasks | (open Cowork → "Scheduled" sidebar) |

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
python3 run.py --status
python3 run.py --mode daily --verbose   # manual catch-up
```

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
