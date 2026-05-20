"""Auto-discover bank filings via NSE's corporate-announcements API.

Every NSE-listed bank (all 20 in scope) is required by SEBI to file investor
presentations, annual reports, earnings transcripts, and financial results as
formal "corporate announcements". NSE exposes these as a JSON API and serves
each attachment from `nsearchives.nseindia.com/corporate/<filename>.pdf`.

Why this matters: it removes the human-maintenance dependency for JS-rendered
IR pages (HDFC, ICICI, SBI). NSE is one uniform source for all 20 banks.

NSE applies bot detection, so we:
  1. Warm up by GETting the public landing page to acquire `nseappid` and
     bm_sv session cookies.
  2. Use a real-browser User-Agent and the Referer that the website itself sets.
  3. Hit the announcements API.
  4. Resolve each attachment URL against `nsearchives.nseindia.com`.

If NSE blocks the request (HTTP 401/403) the engine logs a warning and
returns an empty list — IR page scraping (and Playwright fallback) still runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote_plus, urljoin

from .discover import DiscoveredLink

log = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
NSE_ARCHIVES = "https://nsearchives.nseindia.com"
NSE_HOMEPAGE = NSE_BASE + "/"
# This is the page a real human would have open before the API fires.
NSE_REFERER_TEMPLATE = NSE_BASE + "/companies-listing/corporate-filings-announcements?symbol={symbol}"
NSE_API_TEMPLATE = (
    NSE_BASE + "/api/corporate-announcements"
    "?index=equities&symbol={symbol}&from_date={from_date}&to_date={to_date}"
)

# NSE returns announcement dates as e.g. "18-Oct-2025 17:30:00".
NSE_DATE_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%d-%B-%Y")

# Subject keywords NSE uses for the doc types Rahul cares about.
SUBJECT_PATTERNS = {
    "investor_presentation": re.compile(
        r"(investor[\s-]?present|earnings[\s-]?present|analyst[\s-]?present|earnings[\s-]?update|investor[\s-]?meet)",
        re.IGNORECASE,
    ),
    "transcript": re.compile(
        r"(transcript|conference[\s-]?call|earnings[\s-]?call|concall)",
        re.IGNORECASE,
    ),
    "annual_report": re.compile(
        r"(annual[\s-]?report|integrated[\s-]?annual)",
        re.IGNORECASE,
    ),
    "financial_result": re.compile(
        r"(financial[\s-]?result|board[\s-]?meeting.*result|audited[\s-]?financial|unaudited[\s-]?financial)",
        re.IGNORECASE,
    ),
    "press_release": re.compile(
        r"(press[\s-]?release|press[\s-]?note|media[\s-]?release)",
        re.IGNORECASE,
    ),
}


@dataclass
class NseFiling:
    symbol: str
    subject: str
    description: str
    filing_date: Optional[date]
    attachment_url: Optional[str]
    raw: dict


def _parse_nse_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    for fmt in NSE_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _resolve_attachment(url: str) -> Optional[str]:
    """NSE returns attachment paths in several formats; normalize to absolute https."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        # /corporate/HDFCBANK_18102025...pdf -> nsearchives.nseindia.com/corporate/...
        return NSE_ARCHIVES + url
    return urljoin(NSE_ARCHIVES + "/", url)


def _classify_subject(subject: str, description: str = "") -> Optional[str]:
    haystack = f"{subject} {description}"
    for doc_type, pat in SUBJECT_PATTERNS.items():
        if pat.search(haystack):
            return doc_type
    return None


def warm_up(fetcher) -> bool:
    """Visit the NSE homepage and a Referer page so the session cookie is set.

    Returns True if cookies were acquired. The Fetcher's underlying requests
    Session keeps the cookies on subsequent calls.
    """
    # Homepage
    fr = fetcher.get(NSE_HOMEPAGE, extra_headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    })
    if not fr.ok:
        log.warning("NSE homepage warmup failed (status=%s)", fr.status)
        return False
    # Don't bother fetching the referer page bytes; we only need its cookies, and
    # the API path will set the Referer header itself. Many environments still
    # need this second request to mint the bm_sv cookie though.
    fetcher.get(NSE_BASE + "/companies-listing/corporate-filings-announcements", extra_headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Referer": NSE_HOMEPAGE,
    })
    return True


def fetch_announcements(fetcher, symbol: str, *, history_years: int = 5,
                        today: Optional[date] = None) -> List[NseFiling]:
    """Hit NSE's announcements API for `symbol` over the last N years.

    Returns a flat list of NseFiling. Empty list if NSE blocks or returns
    nothing — caller should treat that as "no NSE data, use IR scraping".
    """
    import json

    today = today or date.today()
    from_d = today.replace(year=today.year - history_years)
    url = NSE_API_TEMPLATE.format(
        symbol=quote_plus(symbol),
        from_date=from_d.strftime("%d-%m-%Y"),
        to_date=today.strftime("%d-%m-%Y"),
    )
    fr = fetcher.get(url, extra_headers={
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_REFERER_TEMPLATE.format(symbol=quote_plus(symbol)),
        "X-Requested-With": "XMLHttpRequest",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    })
    if not fr.ok or not fr.content:
        log.warning("NSE announcements API failed for %s (status=%s)", symbol, fr.status)
        return []
    try:
        data = json.loads(fr.content.decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        log.warning("NSE returned non-JSON for %s: %s", symbol, e)
        return []

    # NSE's response can be either a bare list or {"data": [...]} depending on the
    # endpoint version — handle both.
    rows = data if isinstance(data, list) else data.get("data") or data.get("Table") or []
    out: List[NseFiling] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        subject = (row.get("desc") or row.get("subject") or row.get("subjectVal") or "").strip()
        description = (row.get("attchmntText") or row.get("smTxt") or "").strip()
        att = (row.get("attchmntFile") or row.get("attchmntUrl") or row.get("url") or "").strip()
        dt = _parse_nse_date(row.get("an_dt") or row.get("sort_date") or row.get("dt") or "")
        out.append(NseFiling(symbol=symbol, subject=subject, description=description,
                             filing_date=dt, attachment_url=_resolve_attachment(att), raw=row))
    log.info("NSE: %d announcements for %s in last %d years", len(out), symbol, history_years)
    return out


def filings_to_links(filings: List[NseFiling]) -> List[tuple[DiscoveredLink, str]]:
    """Convert NseFiling -> (DiscoveredLink, type_hint) the orchestrator already consumes.

    The orchestrator's classifier will refine the doc_type using URL + anchor; the
    hint we return here is the NSE subject-based classification.
    """
    out: list[tuple[DiscoveredLink, str]] = []
    for f in filings:
        if not f.attachment_url:
            continue
        hint = _classify_subject(f.subject, f.description) or "mixed"
        anchor = (f.subject or "") + (" — " + f.description if f.description else "")
        out.append((DiscoveredLink(url=f.attachment_url,
                                   anchor_text=anchor.strip(" —"),
                                   source_page="nse",
                                   filing_date=f.filing_date), hint))
    return out
