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

What this adapter does:

  - Loads the page once (no clicks needed — all 3 years already in DOM).
  - Keeps ONLY PDFs whose <a aria-label="Download X"> X is in KEEP_ARIA
    (Investor Presentations + Press Releases, per the project doc-type
    whitelist).
  - Rebuilds each kept anchor with rich text the engine's existing
    classifier can parse cleanly: "<doc-type> Q<n> FY<yy>".
  - Groups by fiscal year and returns one synthetic HTML blob per year
    — so the orchestrator's per-segment consec_seen counter (daily mode)
    works correctly across HDFC's 3 years.

Returning synthetic HTML (instead of overriding classify_link) means we
reuse all the existing classification, history-window, and dedup logic
unchanged. The adapter is purely a discovery-side filter + rewrite.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from urllib.parse import urljoin

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


def render_page(url: str, *, history_years: int, user_agent: str):
    """Engine-side hook. Returns list[(year_label, html_bytes)] — one tuple
    per fiscal year HDFC publishes, newest first.

    The blob for each year is a tiny synthetic HTML document containing
    only the kept anchors with engine-friendly anchor text.
    """
    try:
        from playwright.sync_api import sync_playwright
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


# `classify_link` deliberately not defined — the synthetic anchor text we
# emit above is rich enough for the generic classifier to label everything
# correctly. Keeping the adapter to one hook minimizes maintenance surface.
