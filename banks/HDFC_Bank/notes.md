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

The generic engine path is wrong for HDFC for **four** independent reasons; the adapter fixes all four with one hook (`render_page`):

1. **No native `<select>` for years.** `js_fetcher.fetch_rendered_html_iter_years` looks for a `<select>` to iterate; HDFC uses styled `<button>` pills, so that path silently produces one big un-segmented page.
2. **Anchor text is useless for classification.** Every grid PDF has the same anchor text `(opens in a new tab)` — the generic classifier can't tell an Investor Presentation from a Key Parameters PDF from a Call Transcript. HDFC encodes the doc-type in `aria-label="Download <Category>, File Format: PDF"` instead.
3. **The page has noise.** Outside the grid, the page also exposes 4 footer/sidebar PDFs (Deposit Policy, NPA literature, NPS flyer, etc.). Those have no `aria-label` and would otherwise get misclassified.
4. **The IR page only links the most recent 3 fiscal years** even though HDFC's CDN hosts older quarterly PDFs at predictable URLs. The current IR layout shows FY24/FY25/FY26 — but for FY24 it lists **only press releases**, omitting the IPs even though all 4 FY24 investor-presentation PDFs sit on the CDN at `/content/dam/hdfcbankpws/in/en/pdf/financial-results/2023-2024/quarter-N/q{N}fy24-earnings-presentation.pdf` and respond 200. The generic engine never sees them because nothing on the live site links to them.

What the adapter does:

- Loads the page once (no clicks needed — all linked years pre-rendered).
- Keeps anchors only if `aria-label` starts with `Download Investor Presentations,` or `Download Press Releases,` and the href matches `/financial-results/<YYYY>-<YYYY>/quarter-<N>/`.
- Extracts the fiscal year from the URL path (`2025-2026/` → FY26) and the quarter from `/quarter-N/`.
- **For each fiscal year inside the engine's `history_years` window that didn't get an IP from the IR-page scrape**, speculatively HEADs the predictable CDN URL pattern for that year's 4 quarters and adds any that respond 200. This recovers IPs HDFC stripped from the current IR layout but kept on their CDN — currently the entire FY24 IP set (4 PDFs).
- Returns one synthetic HTML blob per fiscal year, with rich anchor text like `"Investor Presentation Q1 FY26"` — the engine's existing classifier picks the doc-type / FY / Q from that text cleanly.

This keeps the adapter to a single hook (`render_page`) and reuses all of the engine's classification, history-window filtering, dedup, and downloading logic unchanged. There is **no `classify_link` override** — none is needed.

### CDN URL patterns probed

When the IR page is missing an IP for some (year, quarter), the adapter tries these path × filename combinations in order (first 200 wins). Cost: up to 8 HEAD requests per missing (year, quarter); the probe terminates early on each hit.

Path prefixes:
- `/content/dam/hdfcbankpws/in/en/pdf/about-us/financial-results/<YYYY>-<YYYY>/quarter-<N>/` (FY26+ pattern, with `/about-us/`)
- `/content/dam/hdfcbankpws/in/en/pdf/financial-results/<YYYY>-<YYYY>/quarter-<N>/` (FY24/FY25 pattern, no `/about-us/`)

Filename templates:
- `q{N}fy{YY}-earnings-presentation.pdf` (lowercase — FY24, FY26)
- `Q{N}FY{YY}-earnings-presentation.pdf`
- `Q{N}FY{YY}-Earnings-Presentation.pdf` (mixed case — FY25 Q3)
- `Q{N}FY{YY} Earnings Presentation.pdf` (with literal spaces — FY25 Q1)

If HDFC re-uploads FY22/FY23 IPs at a similar path in the future, this probe will pick them up automatically without any code change.

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

- 2026-05-22: IP coverage gap investigation + CDN-probe extension.
  - User asked for "minimum 5 years" of IPs; initial coverage was only FY25 + FY26 (8 IPs).
  - Investigated:
    - HDFC current CDN has FY24 IPs at the predictable `q{N}fy24-earnings-presentation.pdf` path, just not linked from the IR page. **Recoverable.**
    - Legacy `hdfcbank.com` domain → redirects to `hdfc.bank.in`, no separate older site.
    - Internet Archive (wayback) → zero snapshots of HDFC's CDN PDFs.
    - HDFC's sitemap.xml → no references to FY22/FY23 IPs.
    - NSE corporate-announcements API for HDFCBANK → Akamai/IP-blocked from this network at the TLS layer (raw `curl` returns 403). FY22/FY23 IPs are likely there but **not reachable today**.
  - Extended `adapter.py` with `_augment_with_cdn_ip_probe()`: for each FY in the engine's history window where the IR-page scrape missed IPs, speculatively HEADs the predictable CDN URL pattern and adds any 200s.
  - Daily-mode run picked up the 4 FY24 IPs cleanly (`discovered=24, new=4, skipped=20`).
  - **Current coverage: 12 IPs (FY24/FY25/FY26 × 4 quarters), 12 PRs (same).** FY22 and FY23 IPs remain unobtainable without NSE access; the probe is harmless on those years (all 404s).

## URLs tried

- ✅ <https://www.hdfc.bank.in/about-us/investor-relations> — Financial Disclosures → Quarterly Reports. Requires Chrome-class UA (bot UAs get 403). All 3 years pre-rendered.

## Re-running just this bank

```bash
_scheduler/refresh.sh focus HDFC_Bank      # one-time
_scheduler/refresh.sh --mode daily         # quick incremental
_scheduler/refresh.sh --mode backfill --force   # rebuild from scratch
```
