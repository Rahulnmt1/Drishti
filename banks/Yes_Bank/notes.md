# Yes_Bank — operator notes

Last verified: 2026-05-22 (this commit).

## Sources Yes Bank publishes for IR

| What                       | URL                                                                                                  | How we ingest it                                  |
| -------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| Investor Presentation page | https://www.yes.bank.in/about-us/investors-relation/financial-information/investor-presentation     | OCM **items API** via `adapter.py` (no browser)   |
| Press releases             | https://www.yes.bank.in/about-us/media/press-releases                                                | **Not ingested** — see "Press releases" below.    |
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

## How the adapter actually works

Every IP on the IR page is backed by an OCM content item of type
`ybl-mig-investor-presentation-drop-down` (discovered by capturing the XHR
the SPA fires when opening an article-style URL). The published items API
returns all 128 of them in a single call:

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
2.  Calls the items API once → 128 items.
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

## Press releases — intentionally not ingested

The press-releases page (https://www.yes.bank.in/about-us/media/press-releases)
publishes each PR as an **HTML article page** like
`/about-us/media/press-releases-details/<id>-<slug>`, not as a PDF. The
engine is currently PDF-only — there is no code path that fetches an HTML
article and extracts the body. We may add that as a shared engine feature
later (it would also help any other bank that publishes HTML-only PRs).

For now, the `config.json` registers only the IP source. Adding a PR source
later just needs the URL + a new content-type-based query in the adapter
once HTML-article ingestion lands.

## Coverage as of first onboarding run

For the engine's default 5-year window (FY22-FY26):

| Fiscal year | IPs ingested | Notes                                                       |
| ----------- | ------------ | ----------------------------------------------------------- |
| FY2026      | 7            | Q1-Q4 quarterlies + Nov 2025 / Feb 2026 / May 2025 decks    |
| FY2025      | 6            | Q1-Q4 quarterlies + Sep 2024 / Nov 2024 decks               |
| FY2024      | 5            | Q1-Q4 quarterlies + June 2023 deck                          |
| FY2023      | 4            | Q1-Q4 quarterlies                                           |
| FY2022      | 1            | Q4 (March 2022) — see "Why FY2022 only shows 1" below       |
| **Total**   | **23**       |                                                             |

The items API reported 31 IPs across these 5 years; we downloaded 23. The
8 not-downloaded break down as:

- 2 items whose `investor_presentation` HTML field contains no actual PDF
  reference (placeholder content Yes Bank never wired up).
- 6 items the classifier reassigned to an out-of-window fiscal year based
  on their filename. E.g. an item with `year_field = "2021-22"` and a
  filename like `..._december_31_2021.pdf` gets classified as Q3 FY2022,
  which is in window; but a filename like `..._sep_30_2021.pdf` also gets
  classified as Q2 FY2022 (in window) — so the actual reclassification
  loss is smaller than the 6 number suggests. Some FY2022 items have
  filenames whose dates the classifier couldn't unambiguously map to a
  fiscal year, so they default to the calendar-year fiscal year (FY2021,
  which is outside our 5-year window).

### Why FY2022 only shows 1 file

The 4 Yes-Bank items with `year_field = "2021-22"` are:

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
  an `/investor-presentation/<slug>` URL in Firefox with network logging
  (`_scratch/probe_yes.py` has the recipe) and look for the XHR like
  `?q=(type eq "...drop-down" AND Slug eq "...")`. Update `IP_CONTENT_TYPE`
  in `adapter.py`.
- **siteToken not found in IR page HTML.** Yes Bank moved the token to a
  different property. Re-grep the IR page HTML for the hex32 string that
  appears in asset URLs (look at network log for a `…/assets/…?channelToken=`
  request and grep that token in the HTML).
- **Asset URL returns HTTP 401/403.** The channel may have been rotated
  or made private. Re-extract `siteToken` (it's re-extracted every run
  anyway). If still failing, the channel access policy itself changed —
  no recovery without Yes Bank intervention.
