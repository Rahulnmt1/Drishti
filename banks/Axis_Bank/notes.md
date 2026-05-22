# Axis_Bank — notes

Living troubleshooting log for Axis Bank's IR ingestion.

## Page structure

- **Investor Presentations**
  `https://www.axis.bank.in/shareholders-corner/other-information/investor-presentations`
  - **Year picker**: native `<select>` element with 18 fiscal-year options
    (`Select Year`, `2025-2026`, `2024-2025`, …, `2007-2008`). The engine's
    generic `bank_kb/js_fetcher.py` iterates this automatically within the
    `history_years=5` window — **no adapter year-iteration logic needed**.
  - **PDF link source**: direct `<a href="…/<filename>.pdf">` in each
    year-view's rendered DOM. URL pattern:
    `…/investor-presentations/<Y>---<Y+1>/<filename>.pdf` (triple-dash
    separator between start and end calendar years).
  - **Mix of decks**: quarterly IPs (`investor-presentation-q3fy26.pdf`,
    `investor-presentation-for-the-quarter-ended-30th-june-2024.pdf`),
    plus event decks the bank also classifies as investor presentations
    (`axis-bank-esg-presentation-november-2024.pdf`,
    `digital-banking-presentation-at-axis-capital-india-financials-conference---may-2022.pdf`,
    `impact-of-ai-on-banking-presentation-september-2025.pdf`,
    `axis-bank-s-proposed-acquisition-of-citibank-s-consumer-businesses-in-india---march-2022…pdf`).
    We keep all of them — the classifier marks each as
    `investor_presentation`, and the FY iteration places them in the right
    fiscal-year folder.

- **Press Releases**
  `https://www.axis.bank.in/about-us/press-releases`
  - **Year tabs**: 20 plain `<a href="#">` elements (2007-2026), driven by
    `data-pressYear@index="tab"` JS handlers. **They do NOT navigate to
    separate pages or call backend APIs** — clicking them just toggles
    show/hide on already-rendered DOM. The engine does not need to drive
    them.
  - **Month sidebar**: 12 `<a>` elements (January–December) inside a
    `border-l-4 vertical-*` container, same pure-JS UI filter pattern.
  - **PDF link source**: ALL ~245 press releases (2007-2025) are
    pre-rendered in the initial DOM in one fetch. URL pattern:
    `…/default-document-library/press-release/<calendar-year>/<month>/<slug>.pdf`
    — e.g. `…/press-release/2024/october/axis-bank-announces-financial-results-for-the-quarter-ended-30th-september-2024.pdf`.
    The engine's single Playwright render captures every year/month combo.
  - **Page chrome to filter out**: 13 site-wide PDFs (rate cards,
    escalation matrix, customer education literature) live under
    `…/default-document-library/<root>.pdf` (without the `/press-release/`
    segment) — the classifier flags them as `other` and they're dropped
    by `settings.json:doc_types_whitelist`.

## Why there is no `adapter.py`

Both pages turned out to be a clean fit for the generic engine path —
no custom Python logic is necessary. Specifically:

1. **IP page** — the native `<select>` is exactly what
   `js_fetcher.fetch_rendered_html_iter_years()` is built to drive. Five
   year-views × one render each gives complete coverage.
2. **PR page** — the year and month "tabs" are pure CSS toggles over
   already-rendered HTML, so a single render returns every PR in the
   archive. The classifier's URL-prefix corroboration filters out the
   non-press-release `/default-document-library/` chrome.
3. **NSE is disabled** (`use_nse: false`) because Axis's own IR pages
   give exhaustive coverage and NSE adds dozens of board-meeting / analyst
   intimation filings that don't survive the doc-type whitelist anyway
   but cost cookies-warming roundtrips on every refresh.

To make this work for Axis, **two small enhancements to the generic
classifier were needed** — both also benefit any other bank that uses
the same URL conventions:

- `bank_kb/classify.py:detect_period()` learned to read **YEAR/MONTH**
  ordering (`/press-release/2024/october/<slug>.pdf`) and the
  **MONTH-DAY-YEAR** form (`/…april-11-2012.pdf`), in addition to the
  pre-existing MONTH-YEAR pattern.
- `bank_kb/classify.py:classify()` now accepts a `year_label_hint`
  kwarg, used as a fallback FY when the URL/anchor are dateless but
  the link was discovered while iterating a year-shaped `<select>`
  (e.g. the Citibank-acquisition IP, whose filename has no date anywhere
  but was returned by the FY23 selector option). `bank_kb/orchestrator.py`
  parses the iteration label out of the segment name (`#year=2022-2023`)
  and passes it through.

If the bank ever introduces a custom (non-`<select>`) year picker on
the IP page, or changes the PR archive URL pattern, this file should
sprout an `adapter.py` matching the HDFC template at
`banks/HDFC_Bank/adapter.py`.

## Known issues / history

- **2026-05-22 — Onboarded**. Initial 5-year backfill:
  - 63 documents across FY22-FY26 (37 IPs + 23 PRs + 3 financial-result
    quarterly press notices that landed in IPs/FY26 because they were
    discovered via the IP page's static-HTML pass)
  - 1 truly-undated PR remaining:
    `/press-releases/2024-burgundy-private-hurun-india500-national.pdf` —
    a republished ranking notice with **only** "2024" in its slug
    (no month name, no calendar date). Genuinely ambiguous between FY24
    (Jan-Mar 2024) and FY25 (Apr-Dec 2024); the engine prefers
    `undated/` over a guess. Acceptable cost.
  - Pages that the new classifier features rescued from `undated/`:
    - `presentation-on-axis-bank's-acquisition-of-citibank…pdf` — no date
      in filename, recovered via `year_label_hint="2022-2023"` from the
      IP selector iteration → FY23.
    - `axis-bank-opens-its-10000th-atm-april-11-2012.pdf` — date format
      `april-11-2012`, recovered via the new MONTH-DAY-YEAR rule → FY13
      (outside the 5-year window, correctly skipped from download).

## URLs tried

- ✅ `https://www.axis.bank.in/shareholders-corner/other-information/investor-presentations`
- ✅ `https://www.axis.bank.in/about-us/press-releases`
- (NSE corporate-announcements feed for `AXISBANK` not used — `use_nse: false`)
