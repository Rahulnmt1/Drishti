"""Headless-browser HTML fetch via Playwright (optional dependency).

A handful of bank IR pages (HDFC, ICICI, parts of SBI) are heavily client-rendered:
the PDF anchors don't exist in the raw HTML, so requests-based scraping misses
them. When the engine is configured with `requires_js: true` on a source page,
we render the page in a headless Chromium instance and return the resolved HTML.

Many IR pages also paginate by *fiscal year* via a native <select> dropdown
(Axis Bank's investor presentations are the canonical example: only the
currently-selected year's PDFs ever appear in the DOM). For those pages a
single render is not enough — we must select each year option, wait for the
PDF list to re-render, and harvest links from every year in the history
window. `fetch_rendered_html_iter_years` does exactly that.

Playwright is optional. If it isn't installed (or Chromium isn't downloaded),
this module reports unavailable and the orchestrator falls back to seed_urls /
NSE-only discovery.

Install on the user's machine (one-time):
    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

log = logging.getLogger(__name__)


# Regex used to peel digits out of a year-option's label/value:
#   "2025-2026"  -> [2025, 2026]
#   "FY2025"     -> [2025]
#   "FY 2024-25" -> [2024, 25]  -> normalized to [2024, 2025]
#   "2024"       -> [2024]
_DIGIT_RUN_RE = re.compile(r"\d{2,4}")


def is_available() -> tuple[bool, str]:
    """Cheap check: can we import Playwright and is Chromium installed?"""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False, "playwright not installed (pip install playwright; python -m playwright install chromium)"
    return True, "ok"


def fetch_rendered_html(url: str, *, user_agent: str,
                        timeout_seconds: int = 45,
                        wait_for_pdf_links: bool = True) -> Optional[bytes]:
    """Open the URL in headless Chromium, wait for content to render, return HTML.

    `wait_for_pdf_links=True` waits until the page contains at least one
    `<a href*=".pdf">` anchor, up to `timeout_seconds`. Bank IR pages often
    show a spinner first and inject the PDF list after a network call.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.info("js_fetcher: playwright not available; skipping %s", url)
        return None

    log.info("js_fetcher: rendering %s", url)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=user_agent)
                page = ctx.new_page()
                page.set_default_navigation_timeout(timeout_seconds * 1000)
                page.goto(url, wait_until="domcontentloaded")
                if wait_for_pdf_links:
                    try:
                        page.wait_for_selector("a[href*='.pdf' i]", timeout=timeout_seconds * 1000)
                    except PWTimeout:
                        # Some pages link PDFs via JS-driven downloads, not anchors.
                        # Still return the rendered HTML — discover.py also looks at
                        # iframes/embeds, and query-string ?path=...pdf URLs.
                        log.info("js_fetcher: no <a *.pdf> after %ds; returning what we have",
                                 timeout_seconds)
                html = page.content().encode("utf-8")
            finally:
                browser.close()
        return html
    except Exception as e:  # noqa: BLE001
        log.warning("js_fetcher: rendering failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Multi-year ("paginated by fiscal year") renderer
# ---------------------------------------------------------------------------

def _option_end_year(label: str, value: str) -> Optional[int]:
    """Best-effort: extract the *ending* year from a year-option's text/value.

    Examples:
        ("2025-2026", "2025-2026") -> 2026
        ("FY2025",    "FY2025")    -> 2025
        ("2024-25",   "2024-25")   -> 2025
        ("2024",      "2024")      -> 2024

    Returns None if no 2- or 4-digit year-shaped number is found.
    """
    for candidate in (value, label):
        if not candidate:
            continue
        nums = _DIGIT_RUN_RE.findall(candidate)
        years: list[int] = []
        for n in nums:
            if len(n) == 4 and 2000 <= int(n) <= 2099:
                years.append(int(n))
            elif len(n) == 2:
                # Two-digit year — assume 20xx (no Indian bank lists pre-2000 IR docs).
                years.append(2000 + int(n))
        if years:
            return max(years)
    return None


def _current_fy_end_year() -> int:
    """Indian fiscal year ends Mar 31. May 2026 falls in FY 2026-27 -> end year 2027."""
    today = date.today()
    return today.year + 1 if today.month >= 4 else today.year


def _cutoff_end_year(history_years: int, today: Optional[date] = None) -> int:
    """Lowest fiscal-year-end value still inside the history window.

    Mirrors classify.in_history_window: a doc passes if its FY-end-date
    (Mar 31 of fiscal_year) is >= today - history_years. We invert that to
    a minimum-end-year bound so the JS renderer iterates the same set of
    years the classifier will later keep (no wasted year-renders, no
    accidentally-dropped years).
    """
    today = today or date.today()
    cutoff = today.replace(year=today.year - history_years)
    # FY ends March 31. If cutoff falls in Jan–Mar, the FY ending in cutoff.year
    # (on March 31) still satisfies fy_end >= cutoff. Otherwise the earliest
    # FY whose Mar-31 end-date passes is the *next* one.
    return cutoff.year if cutoff.month <= 3 else cutoff.year + 1


def _find_year_select(page, history_years: int):
    """Look for the first native <select> whose options look like fiscal-year
    labels (e.g. "2024-2025", "FY2025", "2024") and that contains at least one
    option within the history window.

    Returns (select_element_handle, current_value, year_options) or None.
    year_options is a list of (option_value, option_label, end_year) tuples.
    """
    cutoff = _cutoff_end_year(history_years)
    try:
        selects = page.query_selector_all("select")
    except Exception:  # noqa: BLE001
        return None

    for sel in selects:
        try:
            opts = sel.query_selector_all("option")
        except Exception:  # noqa: BLE001
            continue
        year_opts: list[tuple[str, str, int]] = []
        for opt in opts:
            try:
                if opt.get_attribute("disabled") is not None:
                    continue
                value = opt.get_attribute("value") or ""
                label = (opt.text_content() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            end_year = _option_end_year(label, value)
            if end_year is None:
                continue
            year_opts.append((value, label, end_year))

        # Heuristic: at least 2 year-shaped options AND ≥1 inside the history
        # window. This avoids treating an unrelated <select> (e.g. language
        # picker, single-year survey) as a fiscal-year selector.
        if len(year_opts) >= 2 and any(y[2] >= cutoff for y in year_opts):
            try:
                current_value = sel.evaluate("el => el.value")
            except Exception:  # noqa: BLE001
                current_value = ""
            return sel, current_value, year_opts
    return None


def fetch_rendered_html_iter_years(
    url: str,
    *,
    user_agent: str,
    history_years: int = 5,
    timeout_seconds: int = 45,
    wait_for_pdf_links: bool = True,
) -> list[tuple[str, bytes]]:
    """Render `url` in headless Chromium and, if the page has a year-shaped
    <select> dropdown, iterate every option inside the history window —
    selecting it, waiting for the PDF list to re-render, and capturing HTML.

    Returns a list of `(year_label, html_blob)` tuples — one entry per visible
    fiscal year. `year_label` is the option's `value` attribute (e.g.
    `"2024-2025"`) when a year selector was found; otherwise an empty string,
    meaning "single render, no year segmentation". Returns `[]` only if
    Playwright isn't installed or the browser failed before producing any
    render at all.

    The orchestrator uses the year_label to scope its daily-mode
    "stop after N consecutive seen" counter — without it, an already-seen
    backlog in the *current* fiscal year could prematurely stop iteration
    before older year-views are even loaded.

    This function is a drop-in replacement for the single-shot
    `fetch_rendered_html`: for pages without a year dropdown it behaves
    identically (returns `[("", html)]`); for pages WITH one it transparently
    backfills every fiscal year inside `history_years`.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.info("js_fetcher: playwright not available; skipping %s", url)
        return []

    log.info("js_fetcher: rendering %s (multi-year aware, history=%dy)", url, history_years)
    blobs: list[tuple[str, bytes]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=user_agent)
                page = ctx.new_page()
                page.set_default_navigation_timeout(timeout_seconds * 1000)
                page.goto(url, wait_until="domcontentloaded")
                if wait_for_pdf_links:
                    try:
                        page.wait_for_selector("a[href*='.pdf' i]", timeout=timeout_seconds * 1000)
                    except PWTimeout:
                        # Page may inject PDFs via iframe/embed instead of <a>.
                        log.info("js_fetcher: no <a *.pdf> after %ds; capturing what we have",
                                 timeout_seconds)

                initial_html = page.content().encode("utf-8")
                found = _find_year_select(page, history_years)
                if found is None:
                    # No year selector — single un-labeled blob, same shape as
                    # fetch_rendered_html. The orchestrator will treat the
                    # whole page as one segment.
                    blobs.append(("", initial_html))
                    return blobs

                sel_handle, current_value, year_options = found
                # Label the initial render with the currently-selected year so
                # downstream segment tracking works uniformly across all blobs.
                blobs.append((current_value or "", initial_html))

                cutoff = _cutoff_end_year(history_years)
                # Sort newest-first so daily-mode "stop after N seen" logic
                # downstream still trips early on a steady-state corpus
                # (within each year-segment).
                year_options = sorted(year_options, key=lambda x: -x[2])
                log.info("js_fetcher: year-select detected on %s — iterating %d option(s) "
                         "within last %dy (cutoff=%d, current=%r)",
                         url,
                         sum(1 for _, _, y in year_options if y >= cutoff),
                         history_years, cutoff, current_value)

                for opt_value, opt_label, end_year in year_options:
                    if end_year < cutoff:
                        continue
                    if opt_value == current_value:
                        # Initial render already covered this year.
                        continue
                    try:
                        # Snapshot existing PDF hrefs so we can detect when
                        # the year-switch actually updates the list (some
                        # sites don't fire `networkidle` reliably).
                        prev_hrefs = page.evaluate(
                            "Array.from(document.querySelectorAll(\"a[href*='.pdf' i]\"))"
                            ".map(e => e.href)"
                        )
                        log.info("js_fetcher: selecting year %r on %s", opt_label, url)
                        sel_handle.select_option(value=opt_value)
                        try:
                            page.wait_for_function(
                                """(prev) => {
                                    const cur = Array.from(
                                        document.querySelectorAll("a[href*='.pdf' i]")
                                    ).map(e => e.href);
                                    if (cur.length === 0) return false;
                                    if (cur.length !== prev.length) return true;
                                    const prevSet = new Set(prev);
                                    return cur.some(h => !prevSet.has(h));
                                }""",
                                arg=prev_hrefs,
                                timeout=15000,
                            )
                        except PWTimeout:
                            log.info("js_fetcher: PDF list didn't change within 15s for year %r "
                                     "on %s; capturing anyway", opt_label, url)
                            page.wait_for_timeout(1500)
                        blobs.append((opt_value, page.content().encode("utf-8")))
                    except Exception as e:  # noqa: BLE001
                        log.warning("js_fetcher: failed to render year %r on %s: %s",
                                    opt_label, url, e)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001
        log.warning("js_fetcher: rendering failed for %s: %s", url, e)
    return blobs
