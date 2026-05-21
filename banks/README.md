# `banks/` — per-bank workspaces

Each bank (or NBFC, insurer, fintech — any tracked entity) gets its own
folder under `banks/`. The folder is the **single source of truth** for that
entity: its IR-page config, any custom Python adapter, and a human-readable
notes file you grow as you learn that bank's quirks.

## What a bank folder looks like

```text
banks/HDFC_Bank/
├── config.json     ← REQUIRED. ticker, sources, requires_js, per-bank doc_types
├── notes.md        ← RECOMMENDED. troubleshooting log for this bank's quirks
└── adapter.py      ← OPTIONAL. only when the generic engine path can't handle this bank
```

The `_template/` folder holds a skeleton you copy when onboarding a new
bank — or just run `python3 add_bank.py HDFC_Bank` and it'll do the copy +
prompt you for the basics.

## `config.json` — required

```json
{
  "name": "HDFC_Bank",
  "ticker": "HDFCBANK",
  "category": "private",
  "sources": [
    {
      "url": "https://www.hdfcbank.com/personal/about-us/investor-relations/financial-results",
      "type_hint": "investor_presentation",
      "requires_js": true
    }
  ],
  "doc_types": ["investor_presentation", "press_release"]
}
```

Fields:

| Field | Required | What it means |
| --- | --- | --- |
| `name` | yes | Folder/identity. Must match the folder name (`banks/<this>/`). |
| `ticker` | optional | NSE symbol. If set, the engine also pulls corporate-announcement filings from NSE for this bank. Omit for unlisted entities. |
| `category` | yes | Free-form tag: `private`, `public`, `nbfc`, `insurance`, `fintech`, … (used for grouping in reports and `--status`). |
| `sources` | yes (≥1) | List of IR-page URLs. Each entry has `url` (required), `type_hint` (optional, used by the classifier as a tie-breaker), and `requires_js` (optional bool — set true for HDFC/ICICI/SBI/Axis-style JS-rendered pages). |
| `doc_types` | optional | Per-bank narrowing of the global whitelist (`settings.json:doc_types_whitelist`). If omitted, defaults to the global list. Lets you say "for HDFC, fetch IPs only — skip press releases". |

## `notes.md` — recommended

A free-form Markdown file where you log everything you learn while
troubleshooting this bank. Future you (and any subagent) will thank you.
Suggested headings:

```markdown
# HDFC_Bank — notes

## Page structure
- IR landing: React SPA, year selector is a custom <button> dropdown (NOT a <select>).
- PDFs load via XHR after year click; no anchor in initial HTML.

## Why the adapter.py exists
- The generic js_fetcher only iterates native <select>; HDFC's dropdown
  is a styled <ul> + <li>. Adapter clicks each <li> and waits for the
  XHR'd <a *.pdf> to appear.

## Known issues
- 2026-04: HDFC changed the dropdown class from `year-picker` to
  `fy-selector`. Updated `adapter.py:_pick_year`.

## URLs tried
- ❌ https://www.hdfcbank.com/about-us/investor-relations  (404 since 2024)
- ✅ https://www.hdfcbank.com/personal/about-us/investor-relations
```

## `adapter.py` — optional

A Python module the engine loads if present. It can override either of two
hooks (define what you need, leave the rest alone):

```python
"""HDFC_Bank custom adapter."""

def render_page(url: str, *, history_years: int, user_agent: str):
    """OPTIONAL OVERRIDE for bank_kb.js_fetcher.fetch_rendered_html_iter_years.

    Return list[tuple[str, bytes]] — same shape: (year_label, html_blob).
    Use this when the generic js_fetcher (which iterates native <select>
    year dropdowns) can't drive the bank's UI — e.g. React-rendered
    custom dropdowns, hover menus, "Load more" infinite scroll, etc.

    If you don't define this function (or return [] / None), the engine
    falls back to the generic renderer.
    """
    from playwright.sync_api import sync_playwright
    blobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_context(user_agent=user_agent).new_page()
            page.goto(url, wait_until="domcontentloaded")
            # ... custom interaction with HDFC's React dropdown ...
            blobs.append(("2025-2026", page.content().encode("utf-8")))
        finally:
            browser.close()
    return blobs


def classify_link(url: str, anchor_text: str, default_meta):
    """OPTIONAL OVERRIDE for bank_kb.classify.classify.

    Return a DocMeta (override the default) or None (accept the default).
    Use this when a bank has filename conventions the generic classifier
    misreads — e.g. HDFC bundles Q1+Q2 in one file named "H1FY25.pdf"
    which the generic 'QnFYnn' regex doesn't match.
    """
    return None  # accept default
```

Adapters are **purely opt-in**. The vast majority of banks won't need one —
the generic engine path (NSE API + IR-page scrape + native-`<select>`
year iteration) handles Axis-style pages perfectly. Adapters exist for
the long tail where a bank invents its own widget.

## Adding a bank

```bash
# Interactive — prompts for ticker, category, and at least one source URL
python3 add_bank.py HDFC_Bank

# Non-interactive
python3 add_bank.py HDFC_Bank \
    --ticker HDFCBANK --category private \
    --source 'https://www.hdfcbank.com/personal/about-us/investor-relations' \
    --requires-js

# Then immediately download 5 years for the new bank
./_scheduler/refresh.sh --mode backfill --bank HDFC_Bank
```

## Removing a bank

Just delete the folder:

```bash
rm -rf banks/HDFC_Bank
```

The engine reads `banks/*/config.json` at the start of every run, so the
removed bank disappears on the next `refresh.sh`. The bank's downloaded
PDFs in `KnowledgeBase/HDFC_Bank/` are *not* auto-cleaned — delete those
manually in Finder if you want.
