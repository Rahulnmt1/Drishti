"""Extract text + light structure from a PDF.

We use pdfplumber because investor decks have heavy tabular content and pdfplumber
preserves layout better than pypdf for the topic-mining use case.

Output is a plain .txt file (one per PDF). The orchestrator stores it next to the
PDF in `extracted_text/`. We also return a few topical flags (digital banking, AI,
channels integration, etc.) that the user explicitly cares about — these go into
the index so they can be filtered/searched without reading every doc.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

log = logging.getLogger(__name__)


# Topical flags Rahul cares about for CXO conversations.
# Keep these patterns broad — we want recall, not precision; filter at query time.
TOPIC_PATTERNS: Dict[str, re.Pattern] = {
    "ai_ml": re.compile(r"\b(artificial intelligence|machine learning|gen[\s\-]?ai|generative ai|llm|"
                        r"large language model|chatbot|conversational ai|ml model|ai[-/ ]powered)\b", re.I),
    "digital_banking": re.compile(r"\b(digital banking|digital channel|mobile banking|net banking|"
                                  r"internet banking|digital transformation|digital first|digital onboarding|"
                                  r"video kyc|v-?kyc)\b", re.I),
    "channels_integration": re.compile(r"\b(omni[\s\-]?channel|channel integration|api banking|open banking|"
                                       r"banking[\s\-]?as[\s\-]?a[\s\-]?service|baas|fintech partnership|"
                                       r"co[\s\-]?lend|account aggregator)\b", re.I),
    "core_banking": re.compile(r"\b(core banking|cbs|finacle|flexcube|t24|temenos|core system|"
                               r"core modernization|core upgrade|core replatform)\b", re.I),
    "cloud_infra": re.compile(r"\b(public cloud|private cloud|hybrid cloud|cloud migration|"
                              r"aws|azure|gcp|google cloud|oracle cloud)\b", re.I),
    "data_analytics": re.compile(r"\b(data analytics|data lake|data warehouse|data platform|"
                                 r"customer 360|cdp|real[\s\-]?time analytics)\b", re.I),
    "cyber_security": re.compile(r"\b(cyber[\s\-]?security|information security|fraud detection|"
                                 r"fraud prevention|risk management|zero trust)\b", re.I),
    "payments": re.compile(r"\b(upi|imps|neft|rtgs|cbdc|e[\s\-]?rupee|digital rupee|"
                           r"unified payments|cards|merchant acquiring|payments stack)\b", re.I),
    "retail_journeys": re.compile(r"\b(retail customer|customer journey|customer experience|cx|"
                                  r"hyper[\s\-]?personalization|personalisation|relationship manager)\b", re.I),
    "msme_corporate": re.compile(r"\b(msme|sme|small and medium|corporate banking|wholesale banking|"
                                 r"working capital|supply chain finance|trade finance)\b", re.I),
    "wealth": re.compile(r"\b(wealth management|private banking|aum|hni|priority banking)\b", re.I),
    "esg_sustainability": re.compile(r"\b(esg|sustainability|climate|green finance|sustainable finance)\b", re.I),
}


@dataclass
class ExtractResult:
    page_count: int
    char_count: int
    text: str
    topic_hits: Dict[str, int] = field(default_factory=dict)
    failed: bool = False
    error: str = ""


def extract_pdf(pdf_path: Path) -> ExtractResult:
    """Pull text out of a PDF. Robust against scanned/encrypted/garbage files."""
    try:
        import pdfplumber  # imported lazily so the discover step works without it
    except ImportError as e:
        return ExtractResult(page_count=0, char_count=0, text="", failed=True,
                             error=f"pdfplumber not installed: {e}")
    pages_text: List[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages):
                try:
                    t = page.extract_text() or ""
                except Exception as e:  # noqa: BLE001 - one bad page shouldn't lose the doc
                    log.debug("Page %d of %s extract failed: %s", i, pdf_path.name, e)
                    t = ""
                pages_text.append(t)
        text = "\n\n".join(pages_text).strip()
    except Exception as e:  # noqa: BLE001 - encrypted or corrupt; record and move on
        return ExtractResult(page_count=0, char_count=0, text="", failed=True, error=str(e))

    topic_hits = {k: len(p.findall(text)) for k, p in TOPIC_PATTERNS.items()}
    topic_hits = {k: v for k, v in topic_hits.items() if v > 0}
    return ExtractResult(page_count=len(pages_text), char_count=len(text),
                         text=text, topic_hits=topic_hits, failed=False)


def write_text(out_path: Path, text: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
