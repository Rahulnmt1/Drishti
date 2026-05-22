"""Yes_Bank custom adapter.

Yes Bank's IR pages are an Oracle Sites Cloud (OCM) single-page app fronted
by Akamai bot-protection. Headless Chromium (the engine's default) is rejected
with ERR_HTTP2_PROTOCOL_ERROR, so the generic js_fetcher cannot drive the
page at all. The Investor Presentation page additionally requires the user
to click a "Search" button after selecting a fiscal year in a <select> —
which the engine's <select>-iteration heuristic also doesn't do.

How we side-step the whole browser problem:

  Each IP listed on the page (whether linked as `/pdf?name=foo.pdf` or as
  `/investor-presentation/<slug>`) is backed by an OCM content item of type
  `ybl-mig-investor-presentation-drop-down`. We can enumerate every IP across
  every fiscal year — past and present — with **one** call to the published
  items API:

    GET /sites/web/content/published/api/v1.1/items
        ?q=(type eq "ybl-mig-investor-presentation-drop-down")
        &limit=200
        &fields=all

  The response gives us 128+ items, each with:

    fields.year_field                      — FY label (e.g. "2025-26")
    fields.name                            — display title
    fields.product_research_txt_1          — canonical PDF filename (when set)
    fields.investor_presentation           — HTML with the user-facing link
                                             (parses to a PDF filename for
                                             ~95% of items in the 5-year
                                             window; ~5% reference SCS page
                                             IDs that are placeholders).

  Then a second cheap API lookup per filename resolves it to the actual
  Oracle Content asset URL:

    GET /sites/web/content/published/api/v1.1/items
        ?q=(name eq "<filename>")
      → returns { items: [ { id: "CONT…", … } ] }

    -> GET /sites/web/content/published/api/v1.1/assets/<asset_id>/native/<filename>
           ?download=false&channelToken=<siteToken>

  The siteToken (different from the page-rendering channel token!) is
  embedded as `customProperties.siteToken` in the IR page HTML — pulled
  via one plain requests.get at startup.

What this adapter returns (per the engine's contract):

  list[(year_label, html_bytes)] — one blob per fiscal year in the engine's
  `history_years` window, newest first. Each blob is a tiny synthetic HTML
  document with `<a href="<direct-asset-url>">title</a>` for every IP whose
  asset lookup succeeded. The engine then downloads those URLs with plain
  requests — no Playwright per file.

Coverage notes (as of 2026-05-22):

  - 5-year window (FY22-FY26): 29 of 31 IPs are recoverable. 2 items are
    placeholder content with no PDF link (Yes Bank publishing artefact).
  - The OCM items API is publicly accessible — no auth, no cookies, no
    browser needed.
  - This adapter has no Playwright/Firefox dependency, runs in ~2-3 seconds,
    and is robust against changes to the IR page chrome.

Why this is better than the IR-page Search-click approach we tried first:

  Approach 1 (Firefox + Search-click) discovers only ~2 IPs/year for the
  current year and 0 for older years, because Yes Bank's IR widget exposes
  a small subset and many entries use article-URL slugs we couldn't follow
  reliably. Approach 2 (this one) reads from the same content-item store
  the IR widget itself uses, so we see everything Yes Bank has published —
  including all the items the widget chooses to render as article links
  rather than direct PDF links.
"""

from __future__ import annotations

import datetime as dt
import html as html_mod
import logging
import re
import urllib.parse
from typing import Optional

import requests

log = logging.getLogger(__name__)

# -- Yes Bank OCM constants -------------------------------------------------

IR_PAGE_URL = (
    "https://www.yes.bank.in/about-us/investors-relation/"
    "financial-information/investor-presentation"
)

ITEMS_API = "https://www.yes.bank.in/sites/web/content/published/api/v1.1/items"
ASSETS_URL_TPL = (
    "https://www.yes.bank.in/sites/web/content/published/api/v1.1/assets/"
    "{asset_id}/native/{filename}?download=false&channelToken={token}"
)

# The OCM content type used for IP entries. The SPA queries this exact name
# when rendering an article-URL IP page (captured via Firefox network log
# during onboarding).
IP_CONTENT_TYPE = "ybl-mig-investor-presentation-drop-down"

# The asset-channel token lives at `customProperties.siteToken` inside the
# IR page HTML — re-extracted on every run so a token rotation never breaks
# this adapter silently.
_SITE_TOKEN_RE = re.compile(
    r'"siteToken"\s*:\s*"([a-f0-9]{32})"', re.IGNORECASE
)

# Filename extraction patterns, tried in priority order:
#   1. <a href="…/pdf?name=foo.pdf">  — used by FY24+ entries
#   2. bare filename like "foo.pdf"   — used by `product_research_txt_1`
#                                       and many FY21-FY23 entries
_PDF_NAME_HREF_RE = re.compile(
    r'href=["\'][^"\']*?pdf\?name=([^"\'&]+\.pdf)', re.IGNORECASE
)
_BARE_PDF_RE = re.compile(
    r'\b([a-zA-Z0-9_\-]+\.pdf)\b', re.IGNORECASE
)

# Years to ignore even within history window — these are content items Yes Bank
# tags with a future or invalid year_field. None today; placeholder for tuning.
_BAD_YEAR_LABELS: set[str] = set()


# ---------------------------------------------------------------------------
# Engine hook
# ---------------------------------------------------------------------------

def render_page(url: str, *, history_years: int, user_agent: str):
    """Engine-side hook. The `url` argument is the IP page URL from the bank
    config — we use it for the siteToken extraction. Discovery itself goes
    through the OCM items API, not the page DOM.

    Returns list[(year_label, html_bytes)], newest year first.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })

    site_token = _fetch_site_token(url, session=session)
    if not site_token:
        log.error("Yes Bank: could not extract siteToken from %s; cannot build "
                  "asset URLs", url)
        return None
    log.info("Yes Bank: extracted siteToken (asset-channel) — first 8 chars: %s…",
             site_token[:8])

    items = _list_ip_content_items(session=session)
    if items is None:
        return None
    log.info("Yes Bank: items API returned %d IP content item(s) (all years)",
             len(items))

    # Year window — keep IPs whose fiscal year is within `history_years` of today.
    keep_years = _history_fy_set(history_years)
    in_window = [it for it in items
                 if _item_end_year(it) in keep_years
                 and _item_year_label(it) not in _BAD_YEAR_LABELS]
    log.info("Yes Bank: %d item(s) inside %d-year window (%s)",
             len(in_window), history_years,
             ", ".join(str(y) for y in sorted(keep_years, reverse=True)))

    # Group items by canonical year label, resolve filename + asset URL.
    by_year: dict[str, list[tuple[str, str]]] = {}
    missing = 0
    for it in in_window:
        end_year = _item_end_year(it)
        if end_year is None:
            continue
        filename = _extract_pdf_filename(it)
        if not filename:
            missing += 1
            log.debug("Yes Bank: no PDF filename in item %s (year=%s)",
                      it.get("id"), it.get("fields", {}).get("year_field"))
            continue
        asset_url = _filename_to_asset_url(filename, site_token, session=session)
        if not asset_url:
            missing += 1
            log.warning("Yes Bank: asset-URL resolution failed for %s (item %s)",
                        filename, it.get("id"))
            continue
        title = _item_title(it) or filename
        canonical = f"{end_year - 1}-{end_year}"   # "2025-2026"
        by_year.setdefault(canonical, []).append((asset_url, title))

    if missing:
        log.info("Yes Bank: skipped %d in-window item(s) with no resolvable PDF "
                 "(placeholder/abandoned content on Yes Bank's side)", missing)

    if not by_year:
        log.warning("Yes Bank: zero downloadable IPs after resolution — items "
                    "API or page structure may have changed; see "
                    "banks/Yes_Bank/notes.md")
        return []

    blobs: list[tuple[str, bytes]] = []
    for canonical_label in sorted(by_year.keys(), reverse=True):
        anchors = by_year[canonical_label]
        synthetic = _build_synthetic_html(anchors)
        blobs.append((canonical_label, synthetic.encode("utf-8")))
        log.info("Yes Bank: %s -> %d direct-PDF anchor(s) in blob",
                 canonical_label, len(anchors))
    return blobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_fy_end_year() -> int:
    """End-year of the current Indian fiscal year. e.g. today in May 2026
    -> FY27 (Apr 2026 – Mar 2027) -> 2027."""
    today = dt.date.today()
    return today.year + 1 if today.month >= 4 else today.year


def _history_fy_set(history_years: int) -> set[int]:
    """Set of FY end years for the last `history_years` fiscal years
    (most-recently-completed-or-in-progress, going back). e.g. with
    today=May 2026 and history_years=5: {2026, 2025, 2024, 2023, 2022}."""
    end = _today_fy_end_year() - 1
    return {end - i for i in range(history_years)}


def _item_year_label(item: dict) -> str:
    return (item.get("fields") or {}).get("year_field", "") or ""


def _item_end_year(item: dict) -> Optional[int]:
    """Parse 'YYYY-YY' or 'YYYY-YYYY' from item.fields.year_field -> end year int."""
    label = _item_year_label(item)
    m = re.match(r"^(\d{4})-(\d{2,4})$", label.strip())
    if not m:
        return None
    start = int(m.group(1))
    tail = m.group(2)
    if len(tail) == 4:
        end = int(tail)
    else:
        end = start - (start % 100) + int(tail)
        if end <= start:
            end += 100
    if end != start + 1:
        return None
    return end


def _item_title(item: dict) -> str:
    f = item.get("fields") or {}
    return (f.get("1747901474238_investor_presentation_headers")
            or item.get("name")
            or "").strip()


def _extract_pdf_filename(item: dict) -> Optional[str]:
    """Return the PDF filename referenced by this content item, preferring
    sources in this order:
      1. fields.product_research_txt_1 (canonical, when set)
      2. <a href="…/pdf?name=foo.pdf"> inside fields.investor_presentation
      3. any bare `foo.pdf` token inside the HTML field
    """
    f = item.get("fields") or {}

    canonical = f.get("product_research_txt_1")
    if isinstance(canonical, str) and canonical.lower().strip().endswith(".pdf"):
        return canonical.strip()

    html = f.get("investor_presentation") or ""
    if not isinstance(html, str):
        return None

    m = _PDF_NAME_HREF_RE.search(html)
    if m:
        return urllib.parse.unquote(m.group(1)).strip()

    m = _BARE_PDF_RE.search(html)
    if m:
        return m.group(1).strip()

    return None


def _fetch_site_token(url: str, *, session: requests.Session) -> Optional[str]:
    """The IR page HTML embeds `customProperties.siteToken = "<hex32>"` —
    that's the OCM channel token the SPA uses for asset downloads."""
    try:
        r = session.get(url, timeout=20, headers={"Accept": "text/html,*/*"})
    except Exception as e:  # noqa: BLE001
        log.warning("Yes Bank: siteToken fetch failed: %s", e)
        return None
    if r.status_code != 200:
        log.warning("Yes Bank: siteToken fetch returned HTTP %d", r.status_code)
        return None
    m = _SITE_TOKEN_RE.search(r.text)
    return m.group(1) if m else None


def _list_ip_content_items(*, session: requests.Session) -> Optional[list[dict]]:
    """Single API call: enumerate every IP content item."""
    q = f'(type eq "{IP_CONTENT_TYPE}")'
    api_url = f"{ITEMS_API}?q={urllib.parse.quote(q)}&limit=200&fields=all"
    try:
        r = session.get(api_url, timeout=30)
    except Exception as e:  # noqa: BLE001
        log.error("Yes Bank: items API by type failed: %s", e)
        return None
    if r.status_code != 200:
        log.error("Yes Bank: items API by type returned HTTP %d: %s",
                  r.status_code, r.text[:200])
        return None
    try:
        body = r.json()
    except Exception as e:  # noqa: BLE001
        log.error("Yes Bank: items API by type returned non-JSON: %s", e)
        return None
    items = body.get("items") or []
    if body.get("hasMore"):
        # Defensive: paginate if Yes Bank publishes >200 IPs (currently 128).
        # Cursor-based: re-issue with offset until exhausted.
        offset = len(items)
        while body.get("hasMore"):
            url2 = f"{api_url}&offset={offset}"
            try:
                r = session.get(url2, timeout=30)
                body = r.json()
                items.extend(body.get("items") or [])
                offset = len(items)
            except Exception:  # noqa: BLE001
                break
    return items


def _filename_to_asset_url(filename: str, site_token: str, *,
                           session: requests.Session) -> Optional[str]:
    """Resolve a PDF filename (e.g. yes_bank_investor_presentation_mar_2026.pdf)
    to its direct OCM asset URL by looking it up via the items API."""
    q = f'(name eq "{filename}")'
    api_url = f"{ITEMS_API}?q={urllib.parse.quote(q)}"
    try:
        r = session.get(api_url, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.warning("Yes Bank: items-by-name lookup failed for %s: %s", filename, e)
        return None
    if r.status_code != 200:
        return None
    try:
        items = r.json().get("items") or []
    except Exception:  # noqa: BLE001
        return None
    if not items:
        return None
    asset_id = items[0].get("id")
    if not asset_id:
        return None
    return ASSETS_URL_TPL.format(
        asset_id=asset_id,
        filename=urllib.parse.quote(filename),
        token=site_token,
    )


def _build_synthetic_html(anchors: list[tuple[str, str]]) -> str:
    """Tiny HTML doc with only the kept anchors. The classifier reads anchor
    text + href for type + FY + quarter detection."""
    parts = ["<!doctype html><html><body>"]
    for href, text in anchors:
        parts.append(
            f'<a href="{html_mod.escape(href, quote=True)}">'
            f'{html_mod.escape(text)}</a>'
        )
    parts.append("</body></html>")
    return "".join(parts)
