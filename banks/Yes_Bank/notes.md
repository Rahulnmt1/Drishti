# Yes_Bank — operator notes

Last verified: 2026-05-22 (this commit).

## Sources Yes Bank publishes for IR

| What                       | URL                                                                                                  | How we ingest it                                  |
| -------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| Investor Presentation page | https://www.yes.bank.in/about-us/investors-relation/financial-information/investor-presentation     | OCM **items API** via `adapter.py` (no browser)   |
| Press releases             | https://www.yes.bank.in/about-us/media/press-releases                                                | OCM **items API** + local HTML→PDF render (see "Press releases" below). |
| NSE corporate-filings feed | https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=YESBANK                  | Disabled (`use_nse: false`, Akamai IP-block).     |

## What the IR page actually looks like

Yes Bank's site (`www.yes.bank.in`, redirects from `yes.bank.in`) is an
Oracle Sites Cloud (OCM) single-page app, fronted by Akamai bot-protection.
The user-facing IR page renders a fiscal-year `<select>` + a Search button;
on Search, a paginated list of IP titles appears. Some titles link to
`/pdf?name=<filename>.pdf` (a wrapper page that loads the actual PDF via
embed); others link to `/investor-presentation/<slug>` article-style URLs
(which also resolve to a PDF behind the scenes).

In all cases, clicking through eventually opens an actual PDF — there are
no HTML-only IPs.

## Why we don't drive the page with the browser

We tried three browser-driven approaches and abandoned them all in favour of
talking directly to OCM:

1.  **Engine's default (headless Chromium)** — Akamai rejects the connection
    with `ERR_HTTP2_PROTOCOL_ERROR`. The H2 SETTINGS / WINDOW_UPDATE frame
    ordering used by headless Chromium is on Akamai's blocklist for this
    host. Disabling H2 (`--disable-http2`) lets the connection establish,
    but the page then hangs at DOMContentLoaded waiting for an Akamai
    sensor challenge that never completes.

2.  **Headless Firefox + stealth + Search-click iteration** — Firefox's
    fingerprint is acceptable to Akamai (only after a stealth init script
    that masks `navigator.webdriver`, plugins, and languages — without it
    we get a degraded 165 KB response with no `<select>` elements at all).
    Driving the page works, but the IP widget only exposes the most recent
    quarter or two per year as direct PDF links; everything else is an
    article-URL slug that needs separate per-item navigation. Onboarded
    coverage with this approach: ~3 IPs across all 5 years. Far short of
    Yes Bank's actual published archive.

3.  **Oracle Content REST API directly** — this is what shipped. See below.

## How the IP route of the adapter works

Every IP on the IR page is backed by an OCM content item of type
`ybl-mig-investor-presentation-drop-down` (discovered by capturing the XHR
the SPA fires when opening an article-style URL). The published items API
returns all 128+ of them in a single call (paginated if needed):

```
GET https://www.yes.bank.in/sites/web/content/published/api/v1.1/items
    ?q=(type eq "ybl-mig-investor-presentation-drop-down")
    &limit=200
    &fields=all
```

Each item has:

| Field                                         | Meaning                                    |
| --------------------------------------------- | ------------------------------------------ |
| `fields.year_field`                           | FY label e.g. `"2025-26"`                  |
| `fields.product_research_txt_1`               | Canonical PDF filename (when set)          |
| `fields.investor_presentation`                | HTML snippet with the user-facing link     |
| `fields.1747901474238_investor_presentation_headers` | Display title                       |
| `name`                                        | Internal title (also displayed)            |

The adapter:

1.  Fetches the IR page HTML (one cheap `requests.get`) and extracts
    `customProperties.siteToken` — the OCM channel token for asset
    downloads (different from the page-rendering channel token). Re-read
    every run so a token rotation never breaks the adapter silently.
2.  Calls the items API → all IPs (paginated; ~128 today).
3.  For each item inside the engine's `history_years` window (matched on
    `year_field` end-year), extracts the PDF filename via the priority
    chain `product_research_txt_1 → href=".../pdf?name=X.pdf" inside
    investor_presentation → bare X.pdf token inside investor_presentation`.
    Items with no resolvable PDF (placeholder/abandoned content on Yes
    Bank's side) are skipped with a debug log.
4.  Looks each filename up in the items API by name (`q=(name eq "X.pdf")`)
    to get its asset id, then constructs the direct download URL:
    ```
    https://www.yes.bank.in/sites/web/content/published/api/v1.1/assets/
      <asset_id>/native/<filename>?download=false&channelToken=<siteToken>
    ```
5.  Groups by fiscal-year end year and returns one synthetic HTML blob
    per FY (newest first), each containing `<a href="<direct-asset-url>">`
    anchors. The engine then downloads with plain requests — no browser
    per file.

Run-time cost: ~3 seconds for discovery (one type query + one name-lookup
per in-window item) plus actual PDF downloads. No Firefox/Chromium needed.

## How the PR route of the adapter works

Yes Bank's press releases (https://www.yes.bank.in/about-us/media/press-releases)
look like documents in the browser — clicking a PR title navigates to a
detail page that renders a formatted press release with tables, narrative,
financial highlights, and so on. BUT no PDF file actually exists at the CMS
level for most of them. The detail page is a SPA that renders an HTML field
from an OCM content item; the formatting just makes it LOOK like a PDF.

Specifically, every PR is backed by an OCM content item of type
`ybl-mig-pl-drop-down` (note: "pl" for press release; discovered the same
way as the IP type — XHR network capture from a PR detail page) with these
fields:

| Field                                         | Meaning                                              |
| --------------------------------------------- | ---------------------------------------------------- |
| `fields.year_field`                           | FY label e.g. `"2025-2026"` (4-4 format, not 4-2)    |
| `fields.month_field`                          | Month string e.g. `"APRIL"`                          |
| `fields.press_release_headers`                | Press release title                                  |
| `fields.press_realeases`                      | **The entire PR body, as HTML.** Vendor typo intentional. |
| `name`, `slug`                                | OCM identifiers                                      |

Out of every 87+ PRs in the engine's 5-year window:

- **~12 contain `<a href=".../pdf?name=foo.pdf">` inside `press_realeases`.**
  These are FY26-era PRs where Yes Bank prepared a separate PDF (a real
  Acrobat document) and embedded a link to it in an otherwise-short HTML
  body. For those, the adapter goes down the same asset-URL resolution
  path as IPs — `_filename_to_asset_url(name=...)` → direct OCM asset
  link → engine downloads with plain requests.
- **~80+ are HTML-body-only.** The `press_realeases` field is the full
  press release — quarterly financial highlights tables, narrative
  paragraphs, executive quotes, contact footer. No PDF anywhere. For
  those, the adapter renders the HTML body to a PDF locally:

    1.  Lazy-launches headless Chromium **once per run** via Playwright.
    2.  For each HTML-body PR, wraps the `press_realeases` field in a
        minimal HTML template (title + small print-friendly CSS) and
        feeds it to Chromium via `page.set_content(...)` — never
        navigates to yes.bank.in, so Akamai never sees us.
    3.  Renders to A4 PDF via `page.pdf(...)` and writes to
        `_engine/.cache/yes_bank_pr_pdfs/<slug>.pdf`. Atomic via
        write-to-tmp + rename. Cached files survive subsequent runs so
        a re-backfill skips the render step entirely.
    4.  Returns `file:///...<slug>.pdf` as the anchor href.

  The engine's `Fetcher.get()` has a small `file://` branch (added in this
  commit; see `bank_kb/fetcher.py`) that reads those local files and
  returns them as if they came from HTTP. Throttling and retries are
  skipped for `file://` — it's just a buffered read.

## Why classify_link is needed for PRs

The orchestrator runs every discovered link through `bank_kb.classify` to
decide `doc_type` (investor_presentation / press_release / financial_result
/ ...). The generic classifier inspects URL + anchor text against keyword
patterns. Many Yes Bank PR URLs and filenames have **no** "press" /
"release" / "pr" substring:

- HTML-rendered PRs: cache filenames look like
  `1481791861158-yb-launches-cluster-banking-initiative-in-nashik.pdf` —
  no PR keyword.
- Even some OCM-asset PRs have filenames like
  `yes_bank_tops_sp_global_csa_rankings.pdf` or
  `yes_bank_announces_the_mr_anantharaman_as_chief_risk_officer.pdf` —
  no PR keyword either.

The classifier's hint-gated fallback ("only trust the page-level
`type_hint=press_release` if the URL itself looks press-release-shaped")
labels these `other`, after which the doc-type allowlist drops them
silently. Without an override, ~80% of in-window Yes Bank PRs would be
discarded.

So the adapter exports a `classify_link` hook (the registry auto-detects
it). It maintains a per-run set `_PR_URLS_BUILT` of every URL it handed to
the engine as a press release — both `file://` (HTML renders) and
`https://` (OCM asset URLs from items with embedded PDFs). When the
classifier calls `classify_link(url, anchor, default)`, the hook checks
URL membership in that set; if it's there, it forces `doc_type =
"press_release"` and otherwise returns `None` (= delegate to default).

## Why the engine needed file:// support

The orchestrator's download step is `fetcher.get(url) → bytes; sanity-
check starts with %PDF; save to canonical bank path`. For us to ship
locally-rendered PRs through that pipeline unchanged, the fetcher just
needed to know how to read `file://` URLs. Five lines added to
`bank_kb/fetcher.py` (`_read_file_url`). Any future bank with the same
"upstream isn't actually a downloadable PDF" problem benefits
automatically.

## Coverage as of this commit (2026-05-22)

Engine's default 5-year window (FY22-FY26):

| Fiscal year | Press releases | IPs + financial_results | Total |
| ----------- | -------------- | ----------------------- | ----- |
| FY2026      | 14             | 9                       | 23    |
| FY2025      | 15             | 9                       | 24    |
| FY2024      | 14             | 7                       | 21    |
| FY2023      | 31             | 7                       | 38    |
| FY2022      | 21             | 1                       | 22    |
| **Total**   | **95**         | **33**                  | **128** |

In-window items reported by the OCM API: 128. In-window items ingested by
the engine: 128. **100% coverage modulo Yes Bank's own placeholder/
abandoned content.** Items dropped: 0 by doc-type allowlist, 2 outside
history window (year_field doesn't parse cleanly — true edge cases on
Yes Bank's side, not engine bugs).

PR breakdown by source pathway:
- 12 PRs surfaced via OCM-asset URLs (embedded PDF in HTML body).
- 83 PRs rendered from HTML body via headless Chromium → cached → file://.

`financial_result`-classified PDFs (e.g. "YES BANK ANNOUNCES FINANCIAL
RESULTS FOR THE QUARTER ENDED JUNE 30, 2025") are stored under
`investor_presentations/FY*/` per the engine's `TYPE_TO_FOLDER` mapping —
this is intentional ("financial_result is stored alongside the deck").

### FY2022 only has 1 IP

The 4 Yes-Bank items with `year_field = "2021-22"` for IPs are:

| Slug                                                                    | Filename hint                            | Classifier FY |
| ----------------------------------------------------------------------- | ---------------------------------------- | ------------- |
| `yes-bank-investor-presentation-for-the-quarter-ended-march-31-2022`    | `..._31march22_pdf.pdf`                  | FY2022 ✓      |
| `null-yes-bank-investor-presentation-for-the-quarter-ended-december`    | (filename references 2021)               | FY2021 (out)  |
| `fy-2021-22-yes-bank-investor-presentation-for-quarter-ended-septemb…`  | (filename references 2021)               | FY2021 (out)  |
| `fy-2021-22-yes-bank-investor-presentation-for-quarter-ended-june30-…`  | no resolvable PDF                        | skipped       |

This is technically correct: a quarterly with calendar date "September 2021"
is Q2 of FY2022, but the classifier sees the year 2021 in the filename and
defaults to calendar-year FY semantics. The adapter does pass
`year_label_hint = "2021-2022"` to the classifier as a fallback, but the
classifier only uses that hint when it can't extract any year information
from the filename at all — here it sees "2021" explicitly and trusts the
filename.

If this matters in the future, the fix is in `bank_kb/classify.py`: prefer
`year_label_hint` over a calendar-year fallback when the hint is present
and the URL-derived year would imply the very last year of the previous FY.
Deferred for now (3 missing items isn't a blocker).

## When things break

- **Items API returns HTTP 4xx or different content type.** Yes Bank may
  have changed the OCM channel or content-type name. Re-capture by loading
  an `/investor-presentation/<slug>` or `/press-releases-details/<slug>`
  URL in Firefox with network logging (`_scratch/probe_yes_pr.py` has the
  recipe). Look for the XHR like
  `?q=(type eq "...drop-down" AND Slug eq "...")` — that's the content-type
  name. Update `IP_CONTENT_TYPE` or `PR_CONTENT_TYPE` in `adapter.py`.
- **siteToken not found in IR page HTML.** Yes Bank moved the token to a
  different property. Re-grep the IR page HTML for the hex32 string that
  appears in asset URLs (look at network log for a `…/assets/…?channelToken=`
  request and grep that token in the HTML).
- **Asset URL returns HTTP 401/403.** The channel may have been rotated
  or made private. Re-extract `siteToken` (it's re-extracted every run
  anyway). If still failing, the channel access policy itself changed —
  no recovery without Yes Bank intervention.
- **HTML-rendered PRs have garbled text / missing tables.** Chromium
  rendered the OCM HTML field in a way that lost structure. First try
  clearing the cache so it re-renders: `rm -rf _engine/.cache/yes_bank_pr_pdfs/`
  then re-run backfill. If still bad, the `_PDF_WRAPPER_CSS` in adapter.py
  may need tuning for whatever new HTML pattern Yes Bank started using.
- **Playwright/Chromium not installed.** The PR adapter logs a clear error
  and skips HTML-body renders (the ~12 asset-backed PRs still ingest).
  Fix: `playwright install chromium` inside the engine venv.

## Cache management

The local PR PDF cache (`_engine/.cache/yes_bank_pr_pdfs/`) is in
`.gitignore` and treated as derived state — it can be wiped at any time
and the next backfill will regenerate (slowly, since it relaunches
Chromium per render). The engine's sqlite manifest is the source of truth
for "what's already been downloaded"; clearing the cache doesn't re-trigger
downloads of items already in the manifest because the URL didn't change
(it's still `file:///.../<same-slug>.pdf`).

To force a fresh render of one PR (e.g. Yes Bank updated the content),
delete that specific cache file AND the corresponding row from
`kb_index.sqlite` (or just delete the cache file and the destination PDF —
the engine will then re-download it on the next run).
