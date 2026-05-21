# HDFC_Bank — notes

A living troubleshooting log for HDFC Bank's IR-page quirks. Update as you learn.

## Page structure (as of 2026-05-21)

- IR landing URL: <https://www.hdfc.bank.in/about-us/investor-relations>
- The relevant view is **Financial Disclosures → Quarterly Reports**.
- Layout: a quarterly grid with seven rows and four quarter columns:

  | Row                               | Column: Q1–Q4 of selected FY |
  | --------------------------------- | ---------------------------- |
  | Financial Results                 | PDF                          |
  | Press Releases                    | PDF                          |
  | Key Parameters                    | PDF                          |
  | Investor Presentations            | PDF                          |
  | Call Audio Recordings (Investors) | mp3                          |
  | Call Audio Recordings (Media)     | mp3                          |
  | Call Transcripts                  | PDF                          |

- **Year selector** sits above the grid: pill buttons labelled `FY 2026 / FY 2025 / FY 2024`. Implementation is pure CSS show/hide — **all three years' anchors are present in the initial DOM at page load**. No click is required to expose them.
- HDFC publishes ~3 years on the page (currently FY24/25/26). The engine's `history_years=5` simply yields whatever the page exposes; we don't synthesize older years.

## Why `adapter.py` exists

The generic engine path is wrong for HDFC for three independent reasons; the adapter fixes all three with one hook (`render_page`):

1. **No native `<select>` for years.** `js_fetcher.fetch_rendered_html_iter_years` looks for a `<select>` to iterate; HDFC uses styled `<button>` pills, so that path silently produces one big un-segmented page.
2. **Anchor text is useless for classification.** Every grid PDF has the same anchor text `(opens in a new tab)` — the generic classifier can't tell an Investor Presentation from a Key Parameters PDF from a Call Transcript. HDFC encodes the doc-type in `aria-label="Download <Category>, File Format: PDF"` instead.
3. **The page has noise.** Outside the grid, the page also exposes 4 footer/sidebar PDFs (Deposit Policy, NPA literature, NPS flyer, etc.). Those have no `aria-label` and would otherwise get misclassified.

What the adapter does:

- Loads the page once (no clicks needed — all years pre-rendered).
- Keeps anchors only if `aria-label` starts with `Download Investor Presentations,` or `Download Press Releases,` and the href matches `/financial-results/<YYYY>-<YYYY>/quarter-<N>/`.
- Extracts the fiscal year from the URL path (`2025-2026/` → FY26) and the quarter from `/quarter-N/`.
- Returns one synthetic HTML blob per fiscal year, with rich anchor text like `"Investor Presentation Q1 FY26"` — the engine's existing classifier picks the doc-type / FY / Q from that text cleanly.

This keeps the adapter to a single hook (`render_page`) and reuses all of the engine's classification, history-window filtering, dedup, and downloading logic unchanged. There is **no `classify_link` override** — none is needed.

## Why `use_nse: false`

`banks/HDFC_Bank/config.json` sets `use_nse: false`. Reason: NSE's corporate-announcements feed for HDFCBANK returns ~50 additional filings (earnings-call intimations, BoD meeting notices, analyst-meet schedules, signed scrip-exchange notices, "SE_Intimation_*" duplicates of the IR-grid PDFs). Many link back to `hdfc.bank.in`'s own CDN, so they look "from HDFC" but completely bypass the adapter's filter. They polluted `investor_presentations/FY*/` with dozens of `SE*` / `HDFCBANK_*` files in the first backfill attempt.

HDFC's IR page is curated, exhaustive for IP + PR, and authoritative — NSE adds noise, not coverage. So we turn NSE off for this bank.

If you ever want quarterly Financial Results PDFs too, add `"Financial Results": ("financial_result", "Financial Result")` to `KEEP_ARIA` in `adapter.py` — they're on the same grid and follow the same URL pattern.

## Known issues / history

- 2026-05-21: Initial onboarding.
  - Wrote `adapter.py` (aria-label filter + URL-path FY parser → synthetic HTML per year).
  - Set `use_nse: false` to suppress NSE noise.
  - Forced engine to treat adapter output as authoritative (see `bank_kb/orchestrator.py: adapter_used` flag) — previously the orchestrator would prefer a bigger static-HTML scrape over an adapter's intentionally narrowed output.
  - First clean backfill: discovered=20 (8 IP + 12 PR), in_window=20, new=20, dl_fail=0, ex_fail=0.
  - Note: HDFC didn't publish Investor Presentation PDFs for FY24 in the IR grid (only press releases). Don't be surprised by `FY2024/` having PRs but no IPs.

## URLs tried

- ✅ <https://www.hdfc.bank.in/about-us/investor-relations> — Financial Disclosures → Quarterly Reports. Requires Chrome-class UA (bot UAs get 403). All 3 years pre-rendered.

## Re-running just this bank

```bash
_scheduler/refresh.sh focus HDFC_Bank      # one-time
_scheduler/refresh.sh --mode daily         # quick incremental
_scheduler/refresh.sh --mode backfill --force   # rebuild from scratch
```
