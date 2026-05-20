"""Discover document links from a bank's IR / press-release page.

Every bank's IR page has its own DOM, but they all share one trait: actionable
documents are exposed as <a href="*.pdf"> (occasionally inside iframes, or as JS-driven
links that still render as anchors). We harvest all anchors, filter to PDF-shaped
URLs, and let the classifier decide the document type.

If a page is heavily client-rendered (rare for IR pages but happens on Yes Bank /
Union Bank), the engine logs a warning and the user can re-run after adding a
direct PDF index URL to banks_config.json — or run that specific bank through
Claude in Chrome to harvest links.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urldefrag, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

PDF_HREF_RE = re.compile(r"\.pdf(\?|$|#)", re.IGNORECASE)
# Some banks (HDFC, ICICI) use a CMS that hides the .pdf in a query param.
QUERY_PDF_RE = re.compile(r"(\.pdf|/pdf/|/PDFs?/|=pdf)", re.IGNORECASE)


@dataclass
class DiscoveredLink:
    url: str
    anchor_text: str
    source_page: str
    # Authoritative filing date when available (NSE provides this). Lets the
    # classifier honor the history window even when filenames are dateless.
    filing_date: Optional[date] = None


def discover_pdf_links(html: bytes, base_url: str) -> List[DiscoveredLink]:
    """Return PDF-ish links found on the page.

    We normalize URLs (resolve relative paths, strip fragments) and dedupe by URL.
    Anchor text is preserved because filenames are often opaque (NSE-style filings),
    and the surrounding text is the best classification signal.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001 - lxml not installed? fall back.
        soup = BeautifulSoup(html, "html.parser")

    seen: set[str] = set()
    out: List[DiscoveredLink] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        abs_url = urldefrag(urljoin(base_url, href))[0]
        # Heuristic: must look like a PDF in path OR query string.
        if not (PDF_HREF_RE.search(abs_url) or QUERY_PDF_RE.search(abs_url)):
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)
        anchor = " ".join(a.get_text(" ", strip=True).split())
        out.append(DiscoveredLink(url=abs_url, anchor_text=anchor, source_page=base_url))

    # Some pages also list PDFs as <iframe src> or data-attribs.
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = (tag.get("src") or tag.get("data") or "").strip()
        if src and PDF_HREF_RE.search(src):
            abs_url = urldefrag(urljoin(base_url, src))[0]
            if abs_url not in seen:
                seen.add(abs_url)
                out.append(DiscoveredLink(url=abs_url, anchor_text="(embedded)", source_page=base_url))

    log.info("Discovered %d PDF links on %s", len(out), base_url)
    return out


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def merge_unique(*lists: Iterable[DiscoveredLink]) -> List[DiscoveredLink]:
    seen, out = set(), []
    for lst in lists:
        for link in lst:
            if link.url in seen:
                continue
            seen.add(link.url)
            out.append(link)
    return out
