"""Classify a discovered PDF link into a document type and infer its fiscal date.

We look at three signals:
    1. URL path & filename (most reliable).
    2. Anchor text from the IR page (good for press releases).
    3. type_hint from banks/<Bank>/config.json for the source page (fallback).

Indian banks report on a fiscal year running April–March. Quarter names you'll see
in filenames: Q1FY24, Q2FY25, Q3-FY24, QFY-2024, FY24, FY2024, Q4_2024 etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from urllib.parse import unquote, urlparse

# --- Type detection -----------------------------------------------------------

INVESTOR_PRES_PAT = re.compile(
    r"(investor[-_\s]*pres|earnings[-_\s]*present|analyst[-_\s]*pres|"
    r"earnings[-_\s]*update|earnings[-_\s]*deck|earnings[-_\s]*call[-_\s]*pres|"
    r"investor[-_\s]*update|investor[-_\s]*deck)",
    re.IGNORECASE,
)
TRANSCRIPT_PAT = re.compile(
    r"(transcript|concall|conference[-_\s]*call|earnings[-_\s]*call(?![-_\s]*pres))",
    re.IGNORECASE,
)
ANNUAL_REPORT_PAT = re.compile(
    r"(annual[-_\s]*report|integrated[-_\s]*annual|"
    r"sustainability[-_\s]*report|ar[-_]?fy?\d{2,4})",
    re.IGNORECASE,
)
PRESS_RELEASE_PAT = re.compile(
    r"(press[-_\s]*release|news[-_\s]*release|media[-_\s]*release|press[-_\s]*note)",
    re.IGNORECASE,
)
FIN_RESULTS_PAT = re.compile(
    r"(financial[-_\s]*result|press[-_\s]*table|results[-_\s]*table|"
    r"audited[-_\s]*financial|unaudited[-_\s]*financial)",
    re.IGNORECASE,
)

DOC_TYPES = ("investor_presentation", "annual_report", "transcript", "press_release", "financial_result", "other")


# When the filename/anchor don't match any pattern, we *may* fall back to the
# page-level type_hint configured in banks/<Bank>/config.json. But IR pages frequently
# include unrelated PDFs (footer links: disclaimers, vendor lists, policies,
# books-of-records, etc.) which would otherwise be silently mislabeled as
# whatever the page's hint says. We require the URL itself to *look like* it
# belongs to the hinted category — i.e. the URL path must contain at least one
# of the keywords below — before we accept the hint. Otherwise we label as
# "other" (and the --type filter, if any, will then correctly drop it).
HINT_URL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "investor_presentation": (
        "investor-pres", "investor_pres", "investorpres",
        "investor-update", "investor_update",
        "earnings-pres", "earnings_pres", "earnings-update",
        "analyst-pres", "analyst_pres",
        "/ip-", "/ip_", "-deck-", "_deck_",
    ),
    "annual_report": (
        "annual-report", "annual_report", "annualreport",
        "integrated-annual", "integrated_annual",
        "/ar-fy", "/ar_fy", "/ar-",
        "sustainability-report", "sustainability_report",
    ),
    "transcript": (
        "transcript", "concall", "conference-call", "conference_call",
        "earnings-call", "earnings_call",
    ),
    "press_release": (
        "press-release", "press_release", "pressrelease",
        "news-release", "news_release",
        "media-release", "media_release",
        "press-note", "press_note",
        "/pr-", "/pr_", "/press/",
    ),
    "financial_result": (
        "financial-result", "financial_result", "financialresult",
        "audited-financial", "audited_financial",
        "unaudited-financial", "unaudited_financial",
        "/results/", "results-table", "press-table",
        "quarterly-result", "quarterly_result",
    ),
}


def _url_corroborates_hint(path_and_query: str, hint: str) -> bool:
    """True if the URL itself contains a substring that's strongly associated
    with `hint`. Used to gate the type_hint fallback so footer/site-wide PDFs
    on a hint-tagged IR page don't get mislabeled as that hint's category.
    """
    keywords = HINT_URL_KEYWORDS.get(hint, ())
    if not keywords:
        return False
    needle = path_and_query.lower()
    return any(k in needle for k in keywords)


@dataclass
class DocMeta:
    doc_type: str
    fiscal_year: Optional[int]  # e.g. 2025 means FY25 (Apr 2024 – Mar 2025)
    fiscal_quarter: Optional[int]  # 1..4
    calendar_date: Optional[date]
    title_guess: str


# --- Fiscal-period detection --------------------------------------------------

QFY_PAT = re.compile(r"Q([1-4])[\s_\-]*FY[\s_\-]*((?:19|20)?\d{2})", re.IGNORECASE)
FY_PAT = re.compile(r"\bFY[\s_\-]*((?:19|20)?\d{2})\b", re.IGNORECASE)
ISO_DATE_PAT = re.compile(r"(20\d{2})[\-_/](\d{1,2})[\-_/](\d{1,2})")
MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _normalize_fy(yy: str) -> Optional[int]:
    if not yy:
        return None
    yy = yy.strip()
    if len(yy) == 2:
        return 2000 + int(yy)  # FY24 -> 2024
    if len(yy) == 4:
        return int(yy)
    return None


def detect_period(text: str) -> tuple[Optional[int], Optional[int], Optional[date]]:
    """Return (fiscal_year, fiscal_quarter, calendar_date) best-effort."""
    # explicit ISO date in URL
    m = ISO_DATE_PAT.search(text)
    cdate = None
    if m:
        try:
            cdate = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            cdate = None

    # Month-name + year (e.g., "oct/Q2FY25" or "2024/march/")
    if cdate is None:
        words = re.split(r"[/\-_.\s]+", text.lower())
        for i, w in enumerate(words):
            if w in MONTH_NAMES and i + 1 < len(words):
                yr_match = re.match(r"(20\d{2})", words[i + 1])
                if yr_match:
                    try:
                        cdate = date(int(yr_match.group(1)), MONTH_NAMES[w], 1)
                        break
                    except ValueError:
                        pass

    qm = QFY_PAT.search(text)
    if qm:
        fq = int(qm.group(1))
        fy = _normalize_fy(qm.group(2))
        return fy, fq, cdate

    fm = FY_PAT.search(text)
    if fm:
        fy = _normalize_fy(fm.group(1))
        return fy, None, cdate

    if cdate:
        # Infer FY: Indian FY ends Mar 31. Apr–Dec -> FY = year+1; Jan–Mar -> FY = year.
        fy = cdate.year + 1 if cdate.month >= 4 else cdate.year
        # Quarter: Apr-Jun=Q1, Jul-Sep=Q2, Oct-Dec=Q3, Jan-Mar=Q4.
        m_to_q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}
        return fy, m_to_q.get(cdate.month), cdate

    return None, None, None


# --- Top-level classifier -----------------------------------------------------

def classify(url: str, anchor_text: str = "", type_hint: str = "mixed") -> DocMeta:
    """Decide the document type and fiscal period for a discovered link."""
    parsed = urlparse(url)
    path = unquote(parsed.path + " " + parsed.query)
    haystack = f"{path} {anchor_text}"

    # Order matters: presentation patterns first to avoid 'earnings call' clashes.
    if TRANSCRIPT_PAT.search(haystack):
        doc_type = "transcript"
    elif INVESTOR_PRES_PAT.search(haystack):
        doc_type = "investor_presentation"
    elif ANNUAL_REPORT_PAT.search(haystack):
        doc_type = "annual_report"
    elif PRESS_RELEASE_PAT.search(haystack):
        doc_type = "press_release"
    elif FIN_RESULTS_PAT.search(haystack):
        doc_type = "financial_result"
    else:
        # No filename/anchor evidence. Only trust the page-level type_hint if
        # the URL itself looks like it belongs to that category (e.g. the path
        # contains "investor-pres" for an investor_presentation hint). Without
        # this guard, footer/site-wide PDFs (disclaimers, policies, vendor
        # lists) on a hint-tagged page get silently mislabeled.
        if type_hint in DOC_TYPES and _url_corroborates_hint(path, type_hint):
            doc_type = type_hint
        else:
            doc_type = "other"

    fy, fq, cdate = detect_period(haystack)

    # Title guess: prefer anchor text; if empty, derive from filename.
    title = anchor_text.strip()
    if not title:
        title = unquote(parsed.path.rsplit("/", 1)[-1]).rsplit(".", 1)[0].replace("-", " ").replace("_", " ").strip()
    return DocMeta(doc_type=doc_type, fiscal_year=fy, fiscal_quarter=fq,
                   calendar_date=cdate, title_guess=title or "untitled")


def in_history_window(meta: DocMeta, years: int, today: Optional[date] = None) -> bool:
    """Keep only documents within the last N years (FY-based or calendar-based)."""
    today = today or date.today()
    cutoff = today.replace(year=today.year - years)
    if meta.calendar_date:
        return meta.calendar_date >= cutoff
    if meta.fiscal_year:
        # FY25 -> Apr 2024–Mar 2025
        fy_end = date(meta.fiscal_year, 3, 31)
        return fy_end >= cutoff
    # If we couldn't parse a date at all, assume recent (better to keep than lose).
    return True
