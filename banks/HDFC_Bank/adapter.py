"""HDFC_Bank custom adapter.

HDFC's IR landing page (https://www.hdfc.bank.in/about-us/investor-relations,
"Financial Disclosures" -> "Quarterly Reports") renders a quarterly grid:

    rows    = [Financial Results, Press Releases, Key Parameters,
               Investor Presentations, Call Audio Recordings (Investors),
               Call Audio Recordings (Media), Call Transcripts]
    columns = [Q1FY26, Q2FY26, Q3FY26, Q4FY26]
    + a year selector (FY 2026 / FY 2025 / FY 2024) — pure CSS show/hide,
      all years are in the DOM at page load.

Why the generic engine can't handle this on its own:

  1. The year tabs aren't a native <select>, so js_fetcher's <select>
     year-iteration heuristic doesn't fire.
  2. The PDF anchors in the IR grid all have the same anchor text
     ("(opens in a new tab)") — the URL/anchor classifier can't tell an
     Investor Presentation from a Key Parameter from a Call Transcript.
  3. The page also has 4 footer PDFs (Deposit Policy, NPA literature, etc.)
     that the engine would otherwise pick up as "other" and dump in
     press_releases/.
  4. The IR page only links the most recent 3 fiscal years even though
     HDFC's CDN hosts older PDFs at predictable URLs. E.g. all 4 FY24
     investor-presentation PDFs sit at
       /content/dam/hdfcbankpws/in/en/pdf/financial-results/2023-2024/quarter-N/q{N}fy24-earnings-presentation.pdf
     and respond 200 — they're just unlinked from the current IR page.
     The generic engine never sees them because nothing links to them.

What this adapter does:

  - Loads the page once (no clicks needed — all 3 linked years already in DOM).
  - Keeps ONLY PDFs whose <a aria-label="Download X"> X is in KEEP_ARIA
    (Investor Presentations + Press Releases, per the project doc-type
    whitelist).
  - For each fiscal year inside the engine's history_years window that
    DIDN'T get an IP from the IR-page scrape, speculatively HEADs the
    predictable CDN URL pattern for that year's 4 quarters and adds any
    that respond 200 (see CDN_IP_TEMPLATES). This recovers IPs HDFC
    stripped from the current IR layout but kept on their CDN.
  - Rebuilds each kept anchor with rich text the engine's existing
    classifier can parse cleanly: "<doc-type> Q<n> FY<yy>".
  - Groups by fiscal year and returns one synthetic HTML blob per year
    — so the orchestrator's per-segment consec_seen counter (daily mode)
    works correctly across HDFC's years.

Returning synthetic HTML (instead of overriding classify_link) means we
reuse all the existing classification, history-window, and dedup logic
unchanged. The adapter is purely a discovery-side filter + rewrite.

Coverage notes (as of 2026-05-22):
  - FY24, FY25, FY26 IPs: fully recoverable (FY24 via CDN probe, FY25/FY26
    via IR-page scrape).
  - FY22, FY23 IPs: NOT recoverable from HDFC's infrastructure. They only
    exist on NSE corporate-announcements feed, which is currently blocking
    requests from this network at the Akamai/IP layer.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


# Map HDFC's aria-label text -> the engine doc-type and a human label to
# bake into the synthetic anchor text. Only types in the engine's
# settings.json:doc_types_whitelist need entries here.
KEEP_ARIA: dict[str, tuple[str, str]] = {
    "Investor Presentations": ("investor_presentation", "Investor Presentation"),
    "Press Releases":         ("press_release",         "Press Release"),
    # If you ever want to also fetch Financial Results, uncomment:
    # "Financial Results":      ("financial_result",      "Financial Result"),
}

# HDFC encodes fiscal year in the URL path as "/financial-results/<start>-<end>/quarter-<N>/".
# E.g. "2025-2026/quarter-1" = FY26 Q1.
_PATH_FY_Q = re.compile(
    r"/financial-results/(?P<start>20\d\d)-(?P<end>20\d\d)/quarter-(?P<q>\d)/",
    re.IGNORECASE,
)

# CDN base — the host that serves every IR PDF.
CDN_BASE = "https://www.hdfc.bank.in"

# Path prefix variants observed over different fiscal years:
#   - FY26 uses /about-us/financial-results/ (with /about-us/)
#   - FY24/FY25 use /financial-results/ (without)
_PATH_PREFIXES = (
    "/content/dam/hdfcbankpws/in/en/pdf/about-us/financial-results/{fy_path}/quarter-{q}/",
    "/content/dam/hdfcbankpws/in/en/pdf/financial-results/{fy_path}/quarter-{q}/",
)

# Filename variants observed per fiscal year (case + dash + underscore + spaces):
#   - q1fy26-earnings-presentation.pdf  (FY26 lowercase)
#   - Q3FY25-Earnings-Presentation.pdf  (FY25 mixed case)
#   - Q1FY25 Earnings Presentation.pdf  (FY25 with literal spaces)
#   - q1fy24-earnings-presentation.pdf  (FY24 lowercase)
# Templates listed in order of how often we've seen them; first 200 wins.
CDN_IP_TEMPLATES = (
    "q{q}fy{yy}-earnings-presentation.pdf",
    "Q{q}FY{yy}-earnings-presentation.pdf",
    "Q{q}FY{yy}-Earnings-Presentation.pdf",
    "Q{q}FY{yy} Earnings Presentation.pdf",
)


def render_page(url: str, *, history_years: int, user_agent: str):
    """Engine-side hook. Returns list[(year_label, html_bytes)] — one tuple
    per fiscal year HDFC publishes, newest first.

    The blob for each year is a tiny synthetic HTML document containing
    only the kept anchors with engine-friendly anchor text. Anchors come
    from two sources, in priority order:
      1. PDFs linked from the IR page (canonical source).
      2. PDFs found via predictable CDN URL probe for any (year, quarter)
         IP combination missing from step 1 (recovers older IPs that HDFC
         keeps on their CDN but has unlinked from the current IR layout).
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        log.error("HDFC adapter needs Playwright (`python3 -m playwright install chromium`)")
        return None

    raw_html = _load_page_html(url, user_agent=user_agent)
    if not raw_html:
        return None

    grouped = _collect_kept_anchors(raw_html, base_url=url)
    if not grouped:
        log.warning("HDFC adapter found 0 kept anchors at %s — page structure may have "
                    "changed; check banks/HDFC_Bank/notes.md", url)
        return []

    # CDN-probe step: for each FY in the engine's history window where
    # the IR-page scrape didn't surface 4 IPs, try the predictable URLs.
    added = _augment_with_cdn_ip_probe(grouped, history_years=history_years,
                                       user_agent=user_agent)
    if added:
        log.info("HDFC: CDN probe added %d IP anchor(s) not linked from the current IR page", added)

    # Newest year first so daily-mode early-stop fires on the most-likely-stale segment.
    blobs: list[tuple[str, bytes]] = []
    for end_year in sorted(grouped.keys(), reverse=True):
        anchors = grouped[end_year]
        synthetic = _build_synthetic_html(anchors)
        blobs.append((f"FY{end_year % 100:02d}", synthetic.encode("utf-8")))
        log.info("HDFC: FY%02d -> %d kept anchor(s)", end_year % 100, len(anchors))
    return blobs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_page_html(url: str, *, user_agent: str, timeout_ms: int = 60_000) -> str:
    """Render the page once and return its post-network-idle HTML."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=user_agent,
                                      viewport={"width": 1400, "height": 900})
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # Tiny extra settle for any post-load lazy loads.
            page.wait_for_timeout(1500)
            html = page.content()
        finally:
            browser.close()
    return html


def _collect_kept_anchors(html: str, *, base_url: str
                          ) -> dict[int, list[tuple[str, str, int, str]]]:
    """Parse the page HTML and return:
        { fy_end_year (int) -> [ (abs_url, doc_type, quarter_int, label), ... ] }

    Anchors whose aria-label isn't in KEEP_ARIA are dropped.
    Anchors whose URL doesn't match the /financial-results/<Y>-<Y>/quarter-<N>/
    path are dropped (filters out the footer noise).
    """
    soup = BeautifulSoup(html, "html.parser")
    grouped: dict[int, list[tuple[str, str, int, str]]] = defaultdict(list)
    for a in soup.find_all("a", href=True):
        aria = (a.get("aria-label") or "").strip()
        if not aria.startswith("Download "):
            continue
        # aria looks like 'Download Investor Presentations, File Format: PDF'
        category = aria[len("Download "):].split(",", 1)[0].strip()
        if category not in KEEP_ARIA:
            continue
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        m = _PATH_FY_Q.search(href)
        if not m:
            # Skip anchors that match our aria filter but live somewhere
            # unexpected (e.g. an "RBI Statement" link that gets a 'Download
            # Press Releases'-shaped aria-label outside the quarterly grid).
            continue
        end_year = int(m.group("end"))
        q = int(m.group("q"))
        doc_type, label = KEEP_ARIA[category]
        abs_url = urljoin(base_url, href)
        grouped[end_year].append((abs_url, doc_type, q, label))
    return grouped


def _build_synthetic_html(anchors: list[tuple[str, str, int, str]]) -> str:
    """Construct a minimal HTML doc the engine's discover.py + classify.py
    can parse. Anchor text is engineered so:
      - classify.py matches the doc_type via its existing IP/PR regexes
      - classify.py picks the FY + Q from the rich text (e.g. 'Q1 FY26')

    We intentionally don't set our own type_hint — the URL already encodes
    the fiscal period, and classify.py corroborates type via the anchor
    text (more reliable than a single bank-wide hint).
    """
    out = ['<!doctype html><html><head><meta charset="utf-8"></head><body>',
           '<h1>HDFC Bank IR (filtered by banks/HDFC_Bank/adapter.py)</h1>',
           '<ul>']
    # Sort newest-quarter first within a year, then alphabetical doc-type for stability.
    for abs_url, doc_type, q, label in sorted(anchors,
                                              key=lambda t: (-t[2], t[1])):
        # End-year derived from URL; restate as 'FY<yy>'
        m = _PATH_FY_Q.search(abs_url)
        end_year = int(m.group("end"))
        fy = end_year % 100
        text = f"{label} Q{q} FY{fy:02d}"
        out.append(f'<li><a href="{abs_url}">{text}</a></li>')
    out.append('</ul></body></html>')
    return "\n".join(out)


def _augment_with_cdn_ip_probe(grouped: dict[int, list[tuple[str, str, int, str]]],
                                *, history_years: int, user_agent: str) -> int:
    """For each fiscal year in the engine's history window, look for any
    (year, quarter) IP combo missing from `grouped` and try the predictable
    CDN URL pattern. If a HEAD returns 200, append to `grouped` in-place.

    Returns the number of new anchors added.

    Only IPs are probed. PR filenames on HDFC's CDN are date-based
    (`press-release-march-2026.pdf`) and not derivable from (year, quarter)
    alone, so we don't speculate there — the IR page covers all years
    where HDFC published PRs.
    """
    have_ip_quarters: dict[int, set[int]] = defaultdict(set)
    for end_year, anchors in grouped.items():
        for _, doc_type, q, _ in anchors:
            if doc_type == "investor_presentation":
                have_ip_quarters[end_year].add(q)

    cutoff_end_year, current_end_year = _window_end_year_bounds(history_years)
    label = "Investor Presentation"
    added = 0
    for end_year in range(cutoff_end_year, current_end_year + 1):
        fy_path = f"{end_year - 1}-{end_year}"
        yy = end_year % 100
        for q in (1, 2, 3, 4):
            if q in have_ip_quarters.get(end_year, set()):
                continue
            url = _probe_cdn_for_ip(fy_path=fy_path, q=q, yy=yy, user_agent=user_agent)
            if url:
                grouped[end_year].append((url, "investor_presentation", q, label))
                added += 1
                log.info("HDFC CDN probe: FY%02d Q%d IP found at %s", yy, q, url)
    return added


def _probe_cdn_for_ip(*, fy_path: str, q: int, yy: int, user_agent: str
                      ) -> str | None:
    """Try each (path-prefix × filename-template) combo via HEAD. Return
    the first URL that responds 200, else None.

    Total budget per (year, quarter) is len(_PATH_PREFIXES) * len(CDN_IP_TEMPLATES)
    HEAD requests = 2 * 4 = 8 max. With 5y history × 4 quarters × ~missing
    coverage, this stays under ~25 probes per refresh in practice.
    """
    for prefix in _PATH_PREFIXES:
        path = prefix.format(fy_path=fy_path, q=q)
        for tmpl in CDN_IP_TEMPLATES:
            fname = tmpl.format(q=q, yy=f"{yy:02d}")
            url = CDN_BASE + path + quote(fname)
            if _head_200(url, user_agent):
                return url
    return None


def _head_200(url: str, user_agent: str, *, timeout_s: int = 10) -> bool:
    """True iff a HEAD on `url` returns 200. Quiet on 404 / timeouts / etc."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 200
    except Exception:  # noqa: BLE001  — connection errors, timeouts, etc.
        return False


def _window_end_year_bounds(history_years: int,
                            today: date | None = None) -> tuple[int, int]:
    """Inclusive [cutoff_end_year, current_end_year] for the engine's
    history_years window. Mirrors bank_kb.js_fetcher._cutoff_end_year and
    _current_fy_end_year so the probe stays in sync with what the
    classifier will accept downstream.
    """
    today = today or date.today()
    cutoff = today.replace(year=today.year - history_years)
    cutoff_end_year = cutoff.year if cutoff.month <= 3 else cutoff.year + 1
    current_end_year = today.year + 1 if today.month >= 4 else today.year
    return cutoff_end_year, current_end_year


# `classify_link` deliberately not defined — the synthetic anchor text we
# emit above is rich enough for the generic classifier to label everything
# correctly. Keeping the adapter to one hook minimizes maintenance surface.
