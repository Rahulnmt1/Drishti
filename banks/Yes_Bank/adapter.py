"""Yes_Bank custom adapter.

Yes Bank's IR + media pages are an Oracle Sites Cloud (OCM) single-page app
fronted by Akamai bot-protection. Headless Chromium (the engine's default) is
rejected with ERR_HTTP2_PROTOCOL_ERROR, so the generic js_fetcher cannot drive
either page at all. We side-step the browser problem entirely by querying the
OCM published-items API directly.

Two source URLs, dispatched on URL substring:

1. INVESTOR PRESENTATIONS
   `/about-us/investors-relation/financial-information/investor-presentation`
   Each IP listed on the page (whether linked as `/pdf?name=foo.pdf` or as
   `/investor-presentation/<slug>`) is backed by an OCM content item of type
   `ybl-mig-investor-presentation-drop-down`. One API call enumerates all of
   them across every fiscal year, then a second API lookup per filename
   resolves it to the direct Oracle asset URL.

2. PRESS RELEASES
   `/about-us/media/press-releases`
   Backed by OCM content type `ybl-mig-pl-drop-down`. CRITICAL DIFFERENCE
   from IPs: Yes Bank does NOT publish PR PDFs. The press release content
   (tables, narrative, financial highlights, attribution quotes) is embedded
   directly into the `press_realeases` field (sic — vendor typo) as HTML.
   The detail page in a browser renders that HTML and it LOOKS like a
   formatted document, but no PDF asset exists at the CMS level.

   In our 5-year window (FY22-FY26):
     - ~12 PRs (mostly FY26) contain `<a href=".../pdf?name=foo.pdf">` inside
       the HTML body — these resolve to real OCM assets the same way IPs do.
     - ~75 PRs are HTML-body-only — full content embedded in the field with
       no PDF anywhere. We render those HTML bodies into self-hosted PDFs
       using headless Chromium's page.pdf() (NEVER navigating to yes.bank.in
       — Akamai-safe; we only render our own HTML in-memory) and cache them
       under `_engine/.cache/yes_bank_pr_pdfs/<slug>.pdf`. The cache file
       is then handed to the engine as a `file://` anchor; bank_kb.fetcher
       has a tiny code path to read those.

What this adapter returns (per the engine's contract):

  list[(year_label, html_bytes)] — one blob per fiscal year in the engine's
  `history_years` window, newest first. Each blob is a tiny synthetic HTML
  document with `<a href="...">title</a>` for every recoverable item. The
  engine then downloads those URLs with `Fetcher.get()` — `https://` URLs go
  through the OCM CDN, `file://` URLs are read from local cache. The rest
  of the pipeline (extract → index → store) is identical to every other bank.

Coverage notes (as of 2026-05-22):

  - IPs, 5-year window: 29 of 31 recoverable (2 placeholder items have no
    PDF link — Yes Bank publishing artefact).
  - PRs, 5-year window: 87 in-window items, ~12 with real PDFs + ~75
    rendered from HTML body = ~87 expected. Coverage is materially
    100% modulo OCM items with intentionally empty bodies.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_mod
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# -- Yes Bank OCM constants -------------------------------------------------

IR_PAGE_URL = (
    "https://www.yes.bank.in/about-us/investors-relation/"
    "financial-information/investor-presentation"
)
PR_PAGE_URL = "https://www.yes.bank.in/about-us/media/press-releases"

ITEMS_API = "https://www.yes.bank.in/sites/web/content/published/api/v1.1/items"
ASSETS_URL_TPL = (
    "https://www.yes.bank.in/sites/web/content/published/api/v1.1/assets/"
    "{asset_id}/native/{filename}?download=false&channelToken={token}"
)

# OCM content types. Both names captured from the SPA's own XHR queries
# during onboarding (Firefox network log).
IP_CONTENT_TYPE = "ybl-mig-investor-presentation-drop-down"
PR_CONTENT_TYPE = "ybl-mig-pl-drop-down"

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

# OCM items that are intentionally junk — Yes Bank's CMS has a couple of
# explicitly-named test entries that pollute the items API. Drop by slug
# substring (matched case-insensitively).
_PR_SLUG_BLACKLIST_SUBSTRINGS: tuple[str, ...] = ("test-press-release",)

# Local cache directory for HTML→PDF renders. Relative to the _engine root
# (resolved by walking up from this file: banks/Yes_Bank/adapter.py -> ../../).
_PR_PDF_CACHE_DIR = (Path(__file__).resolve().parent.parent.parent
                     / ".cache" / "yes_bank_pr_pdfs")
_PR_PDF_CACHE_DIR_RESOLVED = str(_PR_PDF_CACHE_DIR.resolve())

# Module-level set of every URL we hand to the engine as a press release —
# both file:// (HTML-body renders) and https:// (OCM asset URLs for items
# that DID have an embedded PDF link in their body). The classify_link hook
# consults this set to force press_release on items whose title/filename
# happens to contain none of the generic classifier's keywords (e.g.
# "yes_bank_tops_sp_global_csa_rankings.pdf", which the engine would
# otherwise label `other` and drop at the doc-type allowlist).
#
# Populated once per render_page invocation (cleared at the top), so this is
# correct as long as classify_link is only called after render_page in the
# same process — which is how the orchestrator runs.
_PR_URLS_BUILT: set[str] = set()


# ---------------------------------------------------------------------------
# Engine hook
# ---------------------------------------------------------------------------

def render_page(url: str, *, history_years: int, user_agent: str):
    """Engine-side hook. Dispatches on `url` substring:
      - IR/investor-presentation page  -> _render_ips()
      - media/press-releases page      -> _render_prs()
    Returns list[(year_label, html_bytes)], newest year first, per the
    standard adapter contract.
    """
    session = _make_session(user_agent)

    # Both routes need the siteToken — fetched from the IR page (the PR page
    # has a different customProperties block; the token is the same per OCM
    # channel, and IR is the historically-stable extraction site).
    site_token = _fetch_site_token(IR_PAGE_URL, session=session)
    if not site_token:
        log.error("Yes Bank: could not extract siteToken from %s; cannot build "
                  "asset URLs", IR_PAGE_URL)
        return None
    log.info("Yes Bank: extracted siteToken (asset-channel) — first 8 chars: %s…",
             site_token[:8])

    if "press-releases" in url:
        # Reset the PR URL tracker at the top of every PR render so a previous
        # IP-only run can't leak state. (The IP route never populates this
        # set so we only need to clear it on the PR route.)
        _PR_URLS_BUILT.clear()
        return _render_prs(history_years=history_years, site_token=site_token,
                           session=session)
    return _render_ips(url, history_years=history_years, site_token=site_token,
                       session=session)


# ---------------------------------------------------------------------------
# Route 1: Investor Presentations
# ---------------------------------------------------------------------------

def _render_ips(url: str, *, history_years: int, site_token: str,
                session: requests.Session):
    items = _list_content_items(IP_CONTENT_TYPE, session=session)
    if items is None:
        return None
    log.info("Yes Bank: items API returned %d IP content item(s) (all years)",
             len(items))

    keep_years = _history_fy_set(history_years)
    in_window = [it for it in items if _item_end_year(it) in keep_years]
    log.info("Yes Bank IPs: %d item(s) inside %d-year window (%s)",
             len(in_window), history_years,
             ", ".join(str(y) for y in sorted(keep_years, reverse=True)))

    by_year: dict[str, list[tuple[str, str]]] = {}
    missing = 0
    for it in in_window:
        end_year = _item_end_year(it)
        if end_year is None:
            continue
        filename = _extract_pdf_filename(it, html_field="investor_presentation",
                                         canonical_field="product_research_txt_1")
        if not filename:
            missing += 1
            log.debug("Yes Bank IP: no PDF filename in item %s (year=%s)",
                      it.get("id"), it.get("fields", {}).get("year_field"))
            continue
        asset_url = _filename_to_asset_url(filename, site_token, session=session)
        if not asset_url:
            missing += 1
            log.warning("Yes Bank IP: asset-URL resolution failed for %s (item %s)",
                        filename, it.get("id"))
            continue
        title = (_ip_item_title(it) or filename)
        canonical = f"{end_year - 1}-{end_year}"
        by_year.setdefault(canonical, []).append((asset_url, title))

    if missing:
        log.info("Yes Bank IPs: skipped %d in-window item(s) with no resolvable "
                 "PDF (placeholder/abandoned content on Yes Bank's side)", missing)

    return _blobs_from_by_year(by_year, log_prefix="Yes Bank IPs")


def _ip_item_title(item: dict) -> str:
    f = item.get("fields") or {}
    return (f.get("1747901474238_investor_presentation_headers")
            or item.get("name")
            or "").strip()


# ---------------------------------------------------------------------------
# Route 2: Press Releases
# ---------------------------------------------------------------------------

def _render_prs(*, history_years: int, site_token: str,
                session: requests.Session):
    items = _list_content_items(PR_CONTENT_TYPE, session=session)
    if items is None:
        return None
    log.info("Yes Bank: items API returned %d PR content item(s) (all years)",
             len(items))

    keep_years = _history_fy_set(history_years)
    in_window: list[dict] = []
    for it in items:
        if _item_end_year(it) not in keep_years:
            continue
        slug = (it.get("slug") or "").lower()
        if any(b in slug for b in _PR_SLUG_BLACKLIST_SUBSTRINGS):
            log.debug("Yes Bank PR: skipping blacklisted slug %s", slug)
            continue
        in_window.append(it)
    log.info("Yes Bank PRs: %d item(s) inside %d-year window after blacklist (%s)",
             len(in_window), history_years,
             ", ".join(str(y) for y in sorted(keep_years, reverse=True)))

    # Two sub-paths:
    #   (a) HTML body contains a real <a href="...pdf"> -> resolve to OCM
    #       asset URL (preferred — Yes Bank's curated PDF).
    #   (b) HTML body is the press release content itself -> render to PDF
    #       locally via headless Chromium, hand engine a file:// URL.
    by_year: dict[str, list[tuple[str, str]]] = {}
    via_asset = 0
    via_render = 0
    failed_render = 0
    no_body = 0

    # Chromium is expensive to launch; defer until we know we need it,
    # launch once, reuse for all renders.
    pdf_renderer: Optional["_HtmlToPdf"] = None

    try:
        for it in in_window:
            end_year = _item_end_year(it)
            if end_year is None:
                continue
            canonical = f"{end_year - 1}-{end_year}"
            slug = it.get("slug") or it.get("id") or ""
            title = _pr_item_title(it) or slug

            filename = _extract_pdf_filename(
                it, html_field="press_realeases", canonical_field=None,
            )
            if filename:
                asset_url = _filename_to_asset_url(filename, site_token,
                                                   session=session)
                if asset_url:
                    by_year.setdefault(canonical, []).append((asset_url, title))
                    _PR_URLS_BUILT.add(asset_url)
                    via_asset += 1
                    continue
                # Asset lookup failed despite a filename being mentioned —
                # fall through to render-from-HTML so we still capture the
                # content (the embedded link is sometimes broken).
                log.debug("Yes Bank PR: asset lookup failed for %s; will render "
                          "HTML body instead", filename)

            html_body = _pr_html_body(it)
            if not html_body:
                no_body += 1
                log.debug("Yes Bank PR: item %s has empty body; skipping", slug)
                continue

            if pdf_renderer is None:
                pdf_renderer = _HtmlToPdf()
                if not pdf_renderer.available():
                    log.error("Yes Bank PR: headless Chromium not available; "
                              "cannot render HTML-body PRs (only the %d "
                              "asset-backed PRs will be ingested)", via_asset)
                    pdf_renderer = None
                    failed_render += 1
                    continue

            safe_slug = _safe_slug(slug)
            cache_path = _PR_PDF_CACHE_DIR / f"{safe_slug}.pdf"
            if not cache_path.is_file():
                ok = pdf_renderer.render(
                    html_body=html_body, title=title, dest=cache_path,
                )
                if not ok:
                    failed_render += 1
                    log.warning("Yes Bank PR: render failed for %s", slug)
                    continue
            file_url = "file://" + urllib.parse.quote(str(cache_path.resolve()))
            by_year.setdefault(canonical, []).append((file_url, title))
            _PR_URLS_BUILT.add(file_url)
            via_render += 1
    finally:
        if pdf_renderer is not None:
            pdf_renderer.close()

    log.info("Yes Bank PRs: anchors built — direct-asset=%d, html-rendered=%d, "
             "render-failed=%d, no-body=%d",
             via_asset, via_render, failed_render, no_body)

    return _blobs_from_by_year(by_year, log_prefix="Yes Bank PRs")


def _pr_item_title(item: dict) -> str:
    f = item.get("fields") or {}
    return (f.get("press_release_headers")
            or item.get("name")
            or "").strip()


def _pr_html_body(item: dict) -> str:
    """The press-release content. Field name has a vendor typo — keep it."""
    f = item.get("fields") or {}
    body = f.get("press_realeases")
    return body if isinstance(body, str) and body.strip() else ""


def _safe_slug(slug: str) -> str:
    """Coerce a slug into a filesystem-safe filename stem. Keep it short
    enough that the eventual canonical path (KnowledgeBase/Yes_Bank/
    press_releases/FY<N>/<stem>.pdf) doesn't blow past macOS's 255-byte
    filename limit."""
    safe = re.sub(r"[^A-Za-z0-9._\-]+", "_", slug).strip("_")
    if not safe:
        safe = "pr_" + hashlib.sha1(slug.encode("utf-8")).hexdigest()[:10]
    return safe[:120]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent or
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        # NB: do NOT advertise 'br' — without brotli installed, `requests`
        # returns the compressed bytes raw and we fail to parse the body.
        "Accept-Encoding": "gzip, deflate",
    })
    return s


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


def _extract_pdf_filename(item: dict, *, html_field: str,
                          canonical_field: Optional[str]) -> Optional[str]:
    """Return the PDF filename referenced by this content item, preferring
    sources in this order:
      1. fields[canonical_field] (when set & is a *.pdf bare name)
      2. <a href="…/pdf?name=foo.pdf"> inside fields[html_field]
      3. any bare `foo.pdf` token inside the HTML field
    """
    f = item.get("fields") or {}

    if canonical_field:
        canonical = f.get(canonical_field)
        if isinstance(canonical, str) and canonical.lower().strip().endswith(".pdf"):
            return canonical.strip()

    html = f.get(html_field) or ""
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


def _list_content_items(content_type: str, *,
                        session: requests.Session) -> Optional[list[dict]]:
    """Enumerate every OCM content item of a given type. Paginates if needed
    — Yes Bank's PR archive has ~500+ items so this WILL paginate.
    """
    q = f'(type eq "{content_type}")'
    base_url = f"{ITEMS_API}?q={urllib.parse.quote(q)}&limit=200&fields=all"
    items: list[dict] = []
    offset = 0
    max_pages = 20  # safety cap: 20 * 200 = 4000 items, more than any bank has
    for _ in range(max_pages):
        url = base_url if offset == 0 else f"{base_url}&offset={offset}"
        try:
            r = session.get(url, timeout=30)
        except Exception as e:  # noqa: BLE001
            log.error("Yes Bank: items API by type=%s failed at offset=%d: %s",
                      content_type, offset, e)
            return None
        if r.status_code != 200:
            log.error("Yes Bank: items API by type=%s returned HTTP %d at "
                      "offset=%d: %s",
                      content_type, r.status_code, offset, r.text[:200])
            return None
        try:
            body = r.json()
        except Exception as e:  # noqa: BLE001
            log.error("Yes Bank: items API returned non-JSON at offset=%d: %s",
                      offset, e)
            return None
        page = body.get("items") or []
        items.extend(page)
        if not body.get("hasMore") or not page:
            return items
        offset = len(items)
    log.warning("Yes Bank: items API for type=%s hit pagination cap (%d items "
                "fetched) — there may be more older items", content_type, len(items))
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


def _blobs_from_by_year(by_year: dict[str, list[tuple[str, str]]], *,
                        log_prefix: str) -> list[tuple[str, bytes]]:
    if not by_year:
        log.warning("%s: zero anchors built — items API or page structure may "
                    "have changed; see banks/Yes_Bank/notes.md", log_prefix)
        return []
    blobs: list[tuple[str, bytes]] = []
    for canonical_label in sorted(by_year.keys(), reverse=True):
        anchors = by_year[canonical_label]
        synthetic = _build_synthetic_html(anchors)
        blobs.append((canonical_label, synthetic.encode("utf-8")))
        log.info("%s: %s -> %d anchor(s) in blob",
                 log_prefix, canonical_label, len(anchors))
    return blobs


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


# ---------------------------------------------------------------------------
# HTML → PDF renderer (used only for press releases without a real PDF)
# ---------------------------------------------------------------------------

# Minimal CSS to keep rendered PDFs readable. Yes Bank's HTML bodies are
# table-heavy and rely on inline styling; we just provide a sane base.
_PDF_WRAPPER_CSS = """
  @page { size: A4; margin: 18mm 14mm; }
  body {
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    font-size: 10.5pt; line-height: 1.45; color: #111;
  }
  h1 {
    font-size: 14pt; margin: 0 0 6mm 0; border-bottom: 1.5pt solid #333;
    padding-bottom: 3mm;
  }
  .meta { color: #666; font-size: 9pt; margin-bottom: 8mm; }
  table { border-collapse: collapse; margin: 4mm 0; max-width: 100%; }
  td, th { padding: 2mm 3mm; border: 0.4pt solid #888; vertical-align: top; }
  ul, ol { padding-left: 6mm; }
  li { margin: 1mm 0; }
  p { margin: 2.5mm 0; }
  strong, b { color: #000; }
"""


class _HtmlToPdf:
    """Lazy Playwright/Chromium wrapper for in-memory HTML→PDF rendering.

    Reused across all PRs in a single adapter invocation (Chromium launch
    costs ~2-3 seconds — amortizing over ~75 renders is meaningful).

    Important: we NEVER navigate to yes.bank.in here. `page.set_content()`
    feeds Chromium HTML we constructed ourselves, so Akamai never sees a
    request from us. This keeps the adapter safe from upstream bot blocks.
    """

    def __init__(self) -> None:
        self._p = None
        self._browser = None
        self._init_failed = False
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            log.error("Yes Bank PR: playwright not installed — cannot render "
                      "HTML body PRs. Install with `pip install playwright "
                      "&& playwright install chromium`.")
            self._init_failed = True
            return
        try:
            from playwright.sync_api import sync_playwright as _sp
            self._p = _sp().start()
            self._browser = self._p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001
            log.error("Yes Bank PR: failed to launch Chromium (%s). Make sure "
                      "`playwright install chromium` has been run.", e)
            self._init_failed = True
            self._cleanup()

    def available(self) -> bool:
        return not self._init_failed and self._browser is not None

    def render(self, *, html_body: str, title: str, dest: Path) -> bool:
        if not self.available():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        full_html = self._wrap(html_body=html_body, title=title)
        ctx = None
        page = None
        try:
            ctx = self._browser.new_context()
            page = ctx.new_page()
            page.set_content(full_html, wait_until="domcontentloaded")
            tmp = dest.with_suffix(".pdf.tmp")
            page.pdf(
                path=str(tmp),
                format="A4",
                print_background=True,
                margin={"top": "18mm", "bottom": "18mm",
                        "left": "14mm", "right": "14mm"},
            )
            tmp.replace(dest)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("Yes Bank PR: render error for %s: %s", dest.name, e)
            return False
        finally:
            try:
                if page is not None:
                    page.close()
                if ctx is not None:
                    ctx.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._p is not None:
                self._p.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._p = None

    @staticmethod
    def _wrap(*, html_body: str, title: str) -> str:
        return (
            "<!doctype html><html><head>"
            '<meta charset="utf-8"><title>'
            f"{html_mod.escape(title)}</title>"
            f"<style>{_PDF_WRAPPER_CSS}</style>"
            "</head><body>"
            f"<h1>{html_mod.escape(title)}</h1>"
            f'<div class="meta">Source: Yes Bank press release '
            f'(rendered from CMS HTML field)</div>'
            f"{html_body}"
            "</body></html>"
        )


# ---------------------------------------------------------------------------
# Engine hook: per-link classification override
# ---------------------------------------------------------------------------

def classify_link(url: str, anchor_text: str, default_meta):
    """Override the generic classifier for links the engine cannot classify
    correctly on its own.

    Reason: every link this adapter emits as a press release lives in
    `_PR_URLS_BUILT`, populated by `_render_prs` for both pathways:
      1. file:// URLs into our local PR PDF cache (HTML-body renders).
      2. OCM https asset URLs for PRs whose body contained an embedded
         <a href="...pdf">.

    The generic classifier examines URL + anchor text against keyword
    patterns. Many Yes Bank PR filenames carry no "press" / "release"
    substring (e.g. `yes_bank_announces_the_mr_anantharaman_as_chief_risk_officer.pdf`
    or our cache slugs like `1481791861158-yb-launches-cluster-banking-...pdf`).
    The hint-gated fallback ("only trust the page-level
    `type_hint=press_release` if the URL itself looks press-release-shaped")
    then labels them `other`, and the doc-type allowlist drops them
    silently. Without this override ~80% of in-window Yes Bank PRs would
    be discarded.

    Scope:
      - Any URL the adapter built for the PR source  -> force press_release.
      - Everything else (IP URLs, NSE filings, etc.) -> keep default
        classification (the generic classifier handles those correctly:
        IP URLs contain `investor_presentation` in the filename, NSE
        attachments come with their own type_hint, etc.).
    """
    if url not in _PR_URLS_BUILT:
        return None

    # Replace doc_type only; keep fiscal_year (resolved from segment year
    # label by the orchestrator), title_guess (from anchor text = real PR
    # title), and any quarter / calendar_date the classifier picked up.
    default_meta.doc_type = "press_release"
    return default_meta
