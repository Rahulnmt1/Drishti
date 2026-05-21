#!/usr/bin/env python3
"""Orchestrator CLI.

Examples:
    # First-time backfill (5 years of history for every registered bank):
    python run.py --mode backfill

    # Daily refresh (skip known docs; stop early once a page is "all known"):
    python run.py --mode daily

    # Single bank, for debugging:
    python run.py --mode backfill --bank HDFC_Bank

    # Focus on a bank first, then operate on it without re-typing --bank:
    ./_scheduler/refresh.sh focus HDFC_Bank
    python run.py --mode daily          # ← picks up the focus

Outputs:
    PDFs under              /<KnowledgeBase>/<Bank>/<type>/FYYY/...
    Plain text under        /<KnowledgeBase>/<Bank>/extracted_text/
    SQLite index at         /<KnowledgeBase>/_engine/kb_index.sqlite
    Run log JSON at         /<KnowledgeBase>/_engine/_logs/run_YYYY-MM-DD_HHMM.json

Per-bank settings live in:
    /<KnowledgeBase>/_engine/banks/<Bank>/config.json   (required)
    /<KnowledgeBase>/_engine/banks/<Bank>/adapter.py    (optional)
    /<KnowledgeBase>/_engine/banks/<Bank>/notes.md      (recommended)

Global engine settings (UA, delays, history_years, doc_types_whitelist) live
in /<KnowledgeBase>/_engine/settings.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from bank_kb.fetcher import Fetcher
from bank_kb.indexer import open_index
from bank_kb.orchestrator import process_bank, TYPE_TO_FOLDER
from bank_kb.registry import (
    BankEntry, adapter_classify_link, adapter_render_page,
    load_all_banks, load_settings,
)
from bank_kb.structure import ensure_bank_structure


HERE = Path(__file__).resolve().parent          # KnowledgeBase/_engine/
KB_ROOT = HERE.parent                            # KnowledgeBase/
LOG_DIR = HERE / "_logs"                         # KnowledgeBase/_engine/_logs/
FOCUS_FILE = HERE / ".kb_focus"                  # KnowledgeBase/_engine/.kb_focus


# CLI input → internal classifier doc_type. Engine fetches only the 3 types
# listed in TYPE_TO_FOLDER (investor_presentation, press_release,
# financial_result); the aliases below let users type folder names too.
TYPE_ALIASES = {
    # folder names (what users see on disk)
    "investor_presentations": "investor_presentation",
    "press_releases":         "press_release",
    "financial_results":      "financial_result",
    # singular forms (classifier output)
    "investor_presentation":  "investor_presentation",
    "press_release":          "press_release",
    "financial_result":       "financial_result",
    # convenience shorthand
    "presentations":          "investor_presentation",
    "presentation":           "investor_presentation",
    "ip":                     "investor_presentation",
    "pr":                     "press_release",
    "fr":                     "financial_result",
}


def _resolve_types(raw_args: list[str]) -> set[str] | None:
    """Normalize --type inputs to a set of classifier doc_type names, or
    None if no filter was requested. Accepts repeated --type and
    comma-separated values.
    """
    if not raw_args:
        return None
    items: list[str] = []
    for arg in raw_args:
        items.extend(s.strip().lower() for s in arg.split(",") if s.strip())
    resolved: set[str] = set()
    invalid: list[str] = []
    for it in items:
        if it in TYPE_ALIASES:
            resolved.add(TYPE_ALIASES[it])
        else:
            invalid.append(it)
    if invalid:
        valid = sorted(set(TYPE_ALIASES.keys()))
        raise SystemExit(
            f"Unknown --type value(s): {invalid}.\n"
            f"Valid options: {', '.join(valid)}"
        )
    return resolved


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


# ---------------------------------------------------------------------------
# Focus mechanism — KB_FOCUS env var > .kb_focus file > nothing
# ---------------------------------------------------------------------------

def _read_focus() -> str | None:
    """Resolve the currently-focused bank, or None if no focus set."""
    env = os.environ.get("KB_FOCUS", "").strip()
    if env:
        return env
    if FOCUS_FILE.exists():
        val = FOCUS_FILE.read_text(encoding="utf-8").strip()
        return val or None
    return None


# ---------------------------------------------------------------------------
# --status reporter
# ---------------------------------------------------------------------------

def _status() -> int:
    """Show last-run state, focus, and corpus stats."""
    from datetime import datetime

    settings = load_settings(HERE)
    banks = load_all_banks(HERE)
    focus = _read_focus()
    focus_source = (
        "env (KB_FOCUS)" if os.environ.get("KB_FOCUS", "").strip()
        else "file (.kb_focus)" if FOCUS_FILE.exists()
        else "<none>"
    )

    print(f"Engine root:    {HERE}")
    print(f"KB root:        {KB_ROOT}")
    print(f"Focused bank:   {focus or '<none>'}  [{focus_source}]")
    print(f"Registered:     {len(banks)} bank(s) — {[b.name for b in banks] or '<none>'}")
    print()

    db = HERE / "kb_index.sqlite"
    if not db.exists():
        print("No SQLite index yet — run `./_scheduler/refresh.sh --mode backfill --bank <X>` first.")
        return 1

    last_log = (max(LOG_DIR.glob("run_*.json"), default=None, key=lambda p: p.stat().st_mtime)
                if LOG_DIR.exists() else None)
    print(f"Index:           {db}  ({db.stat().st_size / 1e6:.1f} MB)")
    if last_log:
        mtime = datetime.fromtimestamp(last_log.stat().st_mtime).astimezone()
        age_hours = (datetime.now().astimezone() - mtime).total_seconds() / 3600
        print(f"Last run:        {mtime.strftime('%Y-%m-%d %H:%M %Z')}  ({age_hours:.1f}h ago)")
        print(f"Last run log:    {last_log}")
        if age_hours > 36:
            print(f"⚠ Stale — last run was {age_hours:.0f} hours ago.")
            print("  Run a manual catch-up:  ./_scheduler/refresh.sh --mode daily")
    else:
        print("Last run:        (no log files found)")

    print()
    with open_index(db) as idx:
        st = idx.stats()
        print(f"Documents indexed: {st['total_documents']}")
        print(f"Doc-type whitelist (settings.json): "
              f"{', '.join(settings.get('doc_types_whitelist', []))}")
        print()
        if st["by_type"]:
            print("By document type:")
            for k, v in sorted(st["by_type"].items(), key=lambda x: -x[1]):
                print(f"  {k:24s} {v}")
        print()
        if st["by_bank"]:
            print("By bank (top 25):")
            for k, v in list(st["by_bank"].items())[:25]:
                print(f"  {k:28s} {v}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build/refresh the Indian banking knowledge base.")
    p.add_argument("--mode", choices=("backfill", "daily"), default="daily")
    p.add_argument("--bank",
                   help="Run a single bank by name (e.g. HDFC_Bank). "
                        "Overrides the focus (.kb_focus / $KB_FOCUS) for this run.")
    p.add_argument("--all-banks", action="store_true",
                   help="Force-run all registered banks even if a focus is set. "
                        "By default a non-empty focus narrows operations to one bank.")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--status", action="store_true",
                   help="Show last-run time, focus, corpus stats — then exit.")
    p.add_argument("--sync-structure", action="store_true",
                   help="Ensure every registered bank has its folder skeleton on "
                        "disk, then exit.")
    p.add_argument("--type", action="append", default=[], dest="types",
                   help="Only fetch docs of this classifier type. Repeat or "
                        "comma-separate to allow several. Accepts folder names "
                        "(investor_presentations, press_releases, financial_results) "
                        "or singular classifier names. Default: settings.json's "
                        "doc_types_whitelist applies (currently IP + PR + FR).")
    p.add_argument("--url", action="append", default=[], dest="urls",
                   help="Fetch from these URL(s) instead of the bank's configured "
                        "sources. Repeat or comma-separate. Requires --bank or focus. "
                        "NSE discovery is also skipped when --url is set.")
    args = p.parse_args(argv)
    types_filter = _resolve_types(args.types)

    setup_logging(args.verbose)
    log = logging.getLogger("run")

    if args.status:
        return _status()

    # ---------- Load registry + settings ----------
    settings = load_settings(HERE)
    focus = _read_focus()
    # CLI --bank wins; otherwise the focused bank narrows operations; otherwise
    # all registered banks. --all-banks lets you opt out of focus for one run.
    if args.bank:
        target = args.bank
    elif focus and not args.all_banks:
        target = focus
        log.info("Using focused bank: %s", target)
    else:
        target = None

    try:
        entries = load_all_banks(HERE, only=target)
    except SystemExit:
        raise

    if not entries:
        log.error(
            "No banks registered under %s/banks/.\n"
            "Add one with:  python3 add_bank.py <BankName>", HERE)
        return 2

    # Auto-heal folder structure for every entry — costs nothing.
    for e in entries:
        if ensure_bank_structure(e.name, KB_ROOT):
            log.info("Created folder skeleton for %s", e.name)
    if args.sync_structure:
        return 0

    fetcher = Fetcher(
        user_agent=settings["user_agent"],
        request_delay_seconds=settings["request_delay_seconds"],
        request_timeout_seconds=settings["request_timeout_seconds"],
        max_pdf_size_mb=settings["max_pdf_size_mb"],
    )
    history_years = settings["history_years"]
    doc_types_whitelist = set(settings["doc_types_whitelist"])
    db_path = HERE / "kb_index.sqlite"

    # ---------- Normalize --url ad-hoc overrides ----------
    adhoc_urls: list[str] = []
    for u in args.urls:
        adhoc_urls.extend(s.strip() for s in u.split(",") if s.strip())
    for u in adhoc_urls:
        if not (u.startswith("http://") or u.startswith("https://")):
            raise SystemExit(f"--url must be http:// or https://, got: {u!r}")
    use_nse_for_run = True
    if adhoc_urls:
        if len(entries) != 1:
            log.error("--url requires exactly one bank (use --bank <X> or kb focus <X>); "
                      "got %d", len(entries))
            return 2
        synthetic_hint = "mixed"
        if types_filter and len(types_filter) == 1:
            synthetic_hint = next(iter(types_filter))
        cfg = dict(entries[0].config)
        cfg["sources"] = [
            {"url": u, "type_hint": synthetic_hint, "requires_js": True}
            for u in adhoc_urls
        ]
        cfg.pop("seed_urls", None)
        entries[0] = BankEntry(config=cfg, adapter=entries[0].adapter,
                               folder=entries[0].folder)
        use_nse_for_run = False
        log.info("Ad-hoc --url override for %s (%d URL%s); NSE skipped",
                 entries[0].name, len(adhoc_urls), "" if len(adhoc_urls) == 1 else "s")

    run_summary = {
        "mode": args.mode,
        "started_at": datetime.utcnow().isoformat(timespec='seconds'),
        "history_years": history_years,
        "focus": focus,
        "types_filter": sorted(types_filter) if types_filter else None,
        "doc_types_whitelist": sorted(doc_types_whitelist),
        "adhoc_urls": adhoc_urls or None,
        "banks": [],
    }
    if types_filter:
        log.info("Type filter active: only fetching %s", ", ".join(sorted(types_filter)))

    nse_warmed = [False]
    with open_index(db_path) as index:
        for entry in entries:
            try:
                stats = process_bank(
                    bank_cfg=entry.config, kb_root=KB_ROOT, fetcher=fetcher, index=index,
                    history_years=history_years, mode=args.mode,
                    use_nse=use_nse_for_run,
                    nse_warmed=nse_warmed,
                    types_filter=types_filter,
                    doc_types_whitelist=doc_types_whitelist,
                    render_page_override=adapter_render_page(entry),
                    classify_link_override=adapter_classify_link(entry),
                )
                run_summary["banks"].append(stats.as_dict())
            except Exception as e:  # noqa: BLE001 - never let one bank kill the run
                log.exception("Bank %s crashed: %s", entry.name, e)
                run_summary["banks"].append({"bank": entry.name, "fatal_error": str(e)})

        run_summary["index_stats"] = index.stats()

    run_summary["ended_at"] = datetime.utcnow().isoformat(timespec='seconds')
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json"
    log_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    log.info("Run summary -> %s", log_path)

    totals = {k: sum(b.get(k, 0) for b in run_summary["banks"])
              for k in ("discovered", "in_window", "new_downloads",
                        "skipped_existing", "skipped_by_type",
                        "download_failures", "extract_failures")}
    log.info("TOTALS: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
