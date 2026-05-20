"""Per-bank orchestration: discover -> filter -> download -> extract -> index.

Two modes:
    backfill  – walks every source page, keeps documents within `history_years`,
                downloads everything not yet in the manifest.
    daily     – same walk, but stops after K consecutive already-seen files per
                source. This makes the daily run cheap because new IR docs are
                always appended to the top of each bank's page.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlparse

from .classify import classify, in_history_window, DocMeta
from .discover import discover_pdf_links, DiscoveredLink
from .extractor import extract_pdf, write_text
from .fetcher import Fetcher
from .indexer import Index
from . import js_fetcher
from . import nse_source

log = logging.getLogger(__name__)

# Map our doc_type to the folder name under each bank.
TYPE_TO_FOLDER = {
    "investor_presentation": "investor_presentations",
    "annual_report": "annual_reports",
    "transcript": "transcripts",
    "press_release": "press_releases",
    "financial_result": "investor_presentations",  # store alongside the deck
    "other": "press_releases",                      # safest default bucket
}


@dataclass
class RunStats:
    bank: str
    discovered: int = 0
    discovered_via_nse: int = 0
    discovered_via_ir: int = 0
    in_window: int = 0
    new_downloads: int = 0
    skipped_existing: int = 0
    skipped_by_type: int = 0
    download_failures: int = 0
    extract_failures: int = 0
    nse_used: bool = False
    js_render_used: int = 0
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {**self.__dict__}


def _safe_filename(url: str, fallback: str) -> str:
    """Derive a friendly filename for the local copy."""
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1] or "")
    name = re.sub(r"[^A-Za-z0-9._\-]+", "_", name)
    if not name.lower().endswith(".pdf"):
        name = re.sub(r"[^A-Za-z0-9._\-]+", "_", fallback) + ".pdf"
    return name[:160]  # keep paths reasonable


def _local_path(kb_root: Path, bank: str, meta: DocMeta, url: str) -> Path:
    folder = TYPE_TO_FOLDER.get(meta.doc_type, "press_releases")
    fy_dir = f"FY{meta.fiscal_year}" if meta.fiscal_year else "undated"
    base = _safe_filename(url, meta.title_guess)
    return kb_root / bank / folder / fy_dir / base


def process_bank(*, bank_cfg: dict, kb_root: Path, fetcher: Fetcher, index: Index,
                 history_years: int, mode: str = "backfill",
                 daily_consec_seen_stop: int = 25,
                 use_nse: bool = True, use_js_render: bool = True,
                 nse_warmed: list[bool] | None = None,
                 types_filter: set[str] | None = None) -> RunStats:
    """Discover from NSE + IR pages (auto-rendered with Playwright where needed),
    then download + extract + index.

    `nse_warmed` is a shared one-element list used to avoid re-warming NSE
    cookies for every bank in the same run.
    """
    bank_name = bank_cfg["name"]
    bank_category = bank_cfg.get("category", "unknown")
    stats = RunStats(bank=bank_name)
    log.info("[%s] starting (%s mode)", bank_name, mode)

    # Links are grouped into ordered, named "segments" instead of one flat list.
    # A segment is a scope inside which daily-mode's "stop after N consecutive
    # already-seen docs" counter is meaningful — i.e. links that share a
    # chronological ordering (newest first). When we cross a segment boundary
    # (NSE -> IR static -> IR year=FY26 -> IR year=FY25 -> ... -> seed) the
    # counter resets, so an already-seen backlog in one year-view cannot
    # prematurely abort iteration over older years.
    all_segments: list[tuple[str, list[tuple[DiscoveredLink, str]]]] = []

    # ---------- Source A: NSE corporate-announcements API (uniform; no manual deps) ----------
    if use_nse and bank_cfg.get("ticker"):
        try:
            if not nse_warmed or not nse_warmed[0]:
                ok = nse_source.warm_up(fetcher)
                if nse_warmed is not None:
                    nse_warmed[0] = ok
            filings = nse_source.fetch_announcements(
                fetcher, bank_cfg["ticker"], history_years=history_years)
            nse_links = nse_source.filings_to_links(filings)
            if nse_links:
                # NSE links come as (link, type_hint) tuples already.
                all_segments.append(("nse", list(nse_links)))
            stats.discovered_via_nse = len(nse_links)
            stats.nse_used = True
        except Exception as e:  # noqa: BLE001 — never let NSE failure kill the run
            log.warning("[%s] NSE discovery failed: %s", bank_name, e)
            stats.errors.append(f"nse_discovery: {e}")

    # ---------- Source B: Bank IR pages (HTML scraping + optional Playwright) ----------
    ir_links_added = 0
    js_available, js_reason = (False, "")
    if use_js_render:
        js_available, js_reason = js_fetcher.is_available()
        if not js_available:
            log.info("[%s] Playwright unavailable: %s", bank_name, js_reason)

    for source in bank_cfg.get("sources", []):
        page_url = source["url"]
        type_hint = source.get("type_hint", "mixed")
        requires_js = bool(source.get("requires_js"))
        log.info("[%s] fetching IR index %s", bank_name, page_url)
        html: bytes | None = None
        fr = fetcher.get(page_url)
        if fr.ok and fr.content:
            html = fr.content
        else:
            stats.errors.append(f"ir_fetch {page_url} status={fr.status}")

        # First pass on whatever HTML we got.
        static_links = discover_pdf_links(html or b"", page_url) if html else []

        # If the page is JS-required (or yielded zero links and we have Playwright),
        # render it and try again. The renderer is also "multi-year aware": if
        # the page paginates PDFs via a native <select> year dropdown (Axis
        # investor presentations / press releases, etc.), it iterates every
        # option inside the history window so we get the full 5-year backfill
        # in one pass — no manual per-year URLs required.
        js_segments: list[tuple[str, list[DiscoveredLink]]] = []
        js_total_unique = 0
        if (requires_js or not static_links) and js_available:
            blobs_with_years = js_fetcher.fetch_rendered_html_iter_years(
                page_url,
                user_agent=fetcher.session.headers.get("User-Agent", ""),
                history_years=history_years,
            )
            if blobs_with_years:
                stats.js_render_used += 1
                # Dedup links *within this page only* (across the year-views),
                # keeping the first year a link is seen in. This way a footer
                # link that's present in every year-view ends up in the first
                # segment (newest year) and won't pollute the segment counter
                # of later years.
                seen_in_this_page: set[str] = set()
                for year_label, blob in blobs_with_years:
                    year_links: list[DiscoveredLink] = []
                    for link in discover_pdf_links(blob, page_url):
                        if link.url in seen_in_this_page:
                            continue
                        seen_in_this_page.add(link.url)
                        year_links.append(link)
                    if year_links:
                        js_segments.append((year_label, year_links))
                        js_total_unique += len(year_links)

        # Use whichever discovery method gave us more unique links — covers
        # the case where a static-HTML page already exposes everything visible
        # and we'd rather skip the JS overhead segmentation altogether.
        if js_total_unique > len(static_links):
            log.info("[%s] js render produced %d link(s) across %d year-view(s); "
                     "promoting over %d link(s) from static HTML on %s",
                     bank_name, js_total_unique, len(js_segments),
                     len(static_links), page_url)
            for year_label, links in js_segments:
                seg_name = (f"ir:{page_url}#year={year_label}"
                            if year_label else f"ir:{page_url}")
                all_segments.append((seg_name, [(l, type_hint) for l in links]))
                ir_links_added += len(links)
        elif static_links:
            all_segments.append((f"ir:{page_url}",
                                 [(l, type_hint) for l in static_links]))
            ir_links_added += len(static_links)

    stats.discovered_via_ir = ir_links_added

    # ---------- Source C: hand-curated seed_urls (rare backstop only) ----------
    seed_links: list[tuple[DiscoveredLink, str]] = []
    for seed in bank_cfg.get("seed_urls", []):
        url = seed["url"] if isinstance(seed, dict) else seed
        hint = (seed.get("type_hint") if isinstance(seed, dict) else None) or "mixed"
        anchor = (seed.get("anchor") if isinstance(seed, dict) else None) or ""
        seed_links.append(
            (DiscoveredLink(url=url, anchor_text=anchor, source_page="seed"), hint))
    if seed_links:
        all_segments.append(("seed", seed_links))

    # Global dedup across segments — banks list the same PDF in multiple places
    # (NSE filing + IR index, IR static + IR rendered, etc.). We keep the
    # first-seen occurrence so segment ordering (and thus the daily-mode
    # newest-first early-stop heuristic) stays intact.
    seen_urls: set[str] = set()
    deduped_segments: list[tuple[str, list[tuple[DiscoveredLink, str]]]] = []
    for seg_name, links in all_segments:
        kept: list[tuple[DiscoveredLink, str]] = []
        for link, hint in links:
            if link.url in seen_urls:
                continue
            seen_urls.add(link.url)
            kept.append((link, hint))
        if kept:
            deduped_segments.append((seg_name, kept))

    stats.discovered = sum(len(s[1]) for s in deduped_segments)

    # ---------- Process each segment with its own consec_seen counter ----------
    for seg_name, seg_links in deduped_segments:
        log.debug("[%s] processing segment %s (%d link(s))",
                  bank_name, seg_name, len(seg_links))
        consec_seen = 0
        for link, hint in seg_links:
            meta = classify(link.url, link.anchor_text, hint)
            # If discover gave us an authoritative filing_date (NSE), use it
            # to backfill the meta and to drive the history-window check.
            # This is how we reliably exclude old filings whose URLs have no
            # date.
            if link.filing_date:
                if not meta.calendar_date:
                    meta.calendar_date = link.filing_date
                if not meta.fiscal_year:
                    # Indian FY ends Mar 31.
                    meta.fiscal_year = link.filing_date.year + 1 if link.filing_date.month >= 4 else link.filing_date.year
                if not meta.fiscal_quarter:
                    m_to_q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}
                    meta.fiscal_quarter = m_to_q.get(link.filing_date.month)
            if not in_history_window(meta, history_years):
                continue

            # Optional type filter — if set, only download docs the classifier
            # labeled with one of the requested types. Counts as a separate
            # skip category (not failure) so summaries are clear.
            if types_filter is not None and meta.doc_type not in types_filter:
                stats.skipped_by_type += 1
                continue

            stats.in_window += 1

            if index.already_downloaded(bank_name, link.url):
                stats.skipped_existing += 1
                consec_seen += 1
                if mode == "daily" and consec_seen >= daily_consec_seen_stop:
                    log.info(
                        "[%s] segment %s: %d consecutive seen; stopping this "
                        "segment early (daily mode) — older segments still process",
                        bank_name, seg_name, consec_seen)
                    break  # next segment still runs (older years, seeds, etc.)
                continue
            consec_seen = 0

            # Download.
            local_path = _local_path(kb_root, bank_name, meta, link.url)
            log.info("[%s] downloading %s -> %s",
                     bank_name, link.url, local_path.relative_to(kb_root))
            fr = fetcher.get(link.url)
            if not fr.ok or not fr.content:
                stats.download_failures += 1
                stats.errors.append(f"download {link.url} status={fr.status}")
                continue
            # Sanity: must look like a PDF.
            if not fr.content.lstrip().startswith(b"%PDF"):
                stats.download_failures += 1
                stats.errors.append(f"not_pdf {link.url}")
                continue
            Fetcher.save(local_path, fr.content)
            sha = Fetcher.sha1(fr.content)
            index.record_download(bank_name, link.url, sha, str(local_path))
            stats.new_downloads += 1

            # Extract + index.
            ex = extract_pdf(local_path)
            if ex.failed:
                stats.extract_failures += 1
                stats.errors.append(f"extract {local_path.name}: {ex.error}")
            text_path = kb_root / bank_name / "extracted_text" / (local_path.stem + ".txt")
            if ex.text:
                write_text(text_path, ex.text)
            index.upsert_document(
                bank=bank_name, bank_category=bank_category, doc_type=meta.doc_type,
                title=meta.title_guess, source_url=link.url, file_path=str(local_path),
                fiscal_year=meta.fiscal_year, fiscal_quarter=meta.fiscal_quarter,
                calendar_date=meta.calendar_date.isoformat() if meta.calendar_date else None,
                page_count=ex.page_count, char_count=ex.char_count,
                topic_hits=ex.topic_hits, sha1=sha, body=ex.text,
            )

    log.info(
        "[%s] done: discovered=%d (nse=%d ir=%d) in_window=%d new=%d skipped=%d "
        "skipped_by_type=%d dl_fail=%d ex_fail=%d js_render=%d",
        bank_name, stats.discovered, stats.discovered_via_nse, stats.discovered_via_ir,
        stats.in_window, stats.new_downloads, stats.skipped_existing,
        stats.skipped_by_type, stats.download_failures, stats.extract_failures,
        stats.js_render_used,
    )
    return stats


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
