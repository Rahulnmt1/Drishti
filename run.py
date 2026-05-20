#!/usr/bin/env python3
"""Orchestrator CLI.

Examples:
    # First-time backfill (5 years of history for every bank):
    python run.py --mode backfill

    # Daily refresh (skip known docs; stop early once a page is "all known"):
    python run.py --mode daily

    # Single bank, for debugging:
    python run.py --mode backfill --bank HDFC_Bank

Outputs:
    PDFs under /<KnowledgeBase>/<Bank>/<type>/FYYY/...
    Plain text under /<KnowledgeBase>/<Bank>/extracted_text/
    SQLite index at  /<KnowledgeBase>/_engine/kb_index.sqlite
    Run log JSON at  /<KnowledgeBase>/_engine/_logs/run_YYYY-MM-DD_HHMM.json

Per-bank folder structure is auto-created at the start of every run from
banks_config.json — so editing the JSON to add a 21st bank (without going
through add_bank.py) still produces a fully-populated tree.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from bank_kb.fetcher import Fetcher
from bank_kb.indexer import open_index
from bank_kb.orchestrator import load_config, process_bank
from bank_kb.structure import ensure_all_banks_structure


HERE = Path(__file__).resolve().parent          # KnowledgeBase/_engine/
KB_ROOT = HERE.parent                            # KnowledgeBase/
LOG_DIR = HERE / "_logs"                         # KnowledgeBase/_engine/_logs/


# CLI input → internal classifier doc_type. Accepts the folder names users
# see on disk (plural) and also the singular classifier names. Add to this
# map if a new classifier type is introduced.
TYPE_ALIASES = {
    # plural folder names (what users see on disk)
    "investor_presentations": "investor_presentation",
    "annual_reports":         "annual_report",
    "transcripts":            "transcript",
    "press_releases":         "press_release",
    "financial_results":      "financial_result",
    # singular forms (classifier output)
    "investor_presentation":  "investor_presentation",
    "annual_report":          "annual_report",
    "transcript":             "transcript",
    "press_release":          "press_release",
    "financial_result":       "financial_result",
    # convenience shorthand
    "presentations":          "investor_presentation",
    "presentation":           "investor_presentation",
    "ar":                     "annual_report",
    "pr":                     "press_release",
}


def _resolve_types(raw_args: list[str]) -> set[str] | None:
    """Normalize --type inputs to a set of classifier doc_type names, or
    None if no filter was requested. Accepts repeated --type and
    comma-separated values: --type ar --type transcripts -> {annual_report, transcript}.
    Raises SystemExit on unknown types.
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


def _status(config_path: Path) -> int:
    """Show last-run state and whether a manual catch-up is recommended."""
    from datetime import datetime, timezone
    from bank_kb.indexer import open_index

    db = KB_ROOT / "_engine" / "kb_index.sqlite"
    if not db.exists():
        print("No index yet — run `python3 run.py --mode backfill --verbose` first.")
        return 1

    # Find most recent run summary.
    last_log = max(LOG_DIR.glob("run_*.json"), default=None, key=lambda p: p.stat().st_mtime) if LOG_DIR.exists() else None
    print(f"Index:           {db}")
    if last_log:
        mtime = datetime.fromtimestamp(last_log.stat().st_mtime).astimezone()
        age_hours = (datetime.now().astimezone() - mtime).total_seconds() / 3600
        print(f"Last run:        {mtime.strftime('%Y-%m-%d %H:%M %Z')}  ({age_hours:.1f}h ago)")
        print(f"Last run log:    {last_log}")
        if age_hours > 36:
            print(f"⚠ Stale — last run was {age_hours:.0f} hours ago.")
            print("  Run a manual catch-up:  python3 run.py --mode daily --verbose")
    else:
        print("Last run:        (no log files found — backfill may not have completed)")

    print()
    with open_index(db) as idx:
        st = idx.stats()
        print(f"Documents indexed: {st['total_documents']}")
        print(f"Banks tracked:     {len(json.loads(Path(config_path).read_text())['banks'])}")
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build/refresh the Indian banking knowledge base.")
    p.add_argument("--mode", choices=("backfill", "daily"), default="daily")
    p.add_argument("--bank", help="Run a single bank by name (e.g. HDFC_Bank).")
    p.add_argument("--config", default=str(HERE / "banks_config.json"))
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--status", action="store_true",
                   help="Show last-run time + corpus stats and exit (no fetch).")
    p.add_argument("--sync-structure", action="store_true",
                   help="Just ensure every bank in banks_config.json has its folder "
                        "skeleton on disk, then exit. Useful after hand-editing the JSON.")
    p.add_argument("--type", action="append", default=[], dest="types",
                   help="Only fetch docs of this classifier type. Repeat or "
                        "comma-separate to allow several. Accepts folder names "
                        "(investor_presentations, annual_reports, transcripts, "
                        "press_releases) or singular classifier names "
                        "(investor_presentation, annual_report, transcript, "
                        "press_release, financial_result). Default: no filter "
                        "(all types are fetched).")
    p.add_argument("--url", action="append", default=[], dest="urls",
                   help="Fetch from these URL(s) instead of the bank's configured "
                        "sources. Repeat or comma-separate. Requires --bank. "
                        "NSE discovery is also skipped when --url is set, so this "
                        "is a clean way to test or pin a single source. "
                        "Synthetic sources use type_hint='mixed' (or your --type "
                        "value if you passed exactly one) and always try "
                        "Playwright rendering.")
    args = p.parse_args(argv)
    types_filter = _resolve_types(args.types)

    # Normalize --url values (comma + repeat).
    adhoc_urls: list[str] = []
    for u in args.urls:
        adhoc_urls.extend(s.strip() for s in u.split(",") if s.strip())
    for u in adhoc_urls:
        if not (u.startswith("http://") or u.startswith("https://")):
            raise SystemExit(f"--url must be http:// or https://, got: {u!r}")
    setup_logging(args.verbose)
    log = logging.getLogger("run")

    if args.status:
        return _status(Path(args.config))

    cfg = load_config(Path(args.config))

    # Always heal folder structure first — costs nothing, makes the engine
    # robust to manual edits of banks_config.json (a 21st bank added by hand
    # gets its folders without anyone remembering to run add_bank.py).
    created = ensure_all_banks_structure(cfg, KB_ROOT)
    if created:
        log.info("Created folder skeleton for %d new bank(s): %s",
                 len(created), ", ".join(created))
    if args.sync_structure:
        return 0
    settings = cfg.get("settings", {})
    fetcher = Fetcher(
        user_agent=settings.get("user_agent", "BankKBBot/1.0"),
        request_delay_seconds=settings.get("request_delay_seconds", 2.0),
        request_timeout_seconds=settings.get("request_timeout_seconds", 45),
        max_pdf_size_mb=settings.get("max_pdf_size_mb", 80),
    )
    history_years = settings.get("history_years", 5)
    db_path = KB_ROOT / "_engine" / "kb_index.sqlite"

    banks = cfg["banks"]
    if args.bank:
        banks = [b for b in banks if b["name"].lower() == args.bank.lower()]
        if not banks:
            log.error("No bank named %r in config", args.bank); return 2

    # --url overrides the bank's configured sources entirely (and skips NSE).
    # Requires --bank because we need to know where the downloads land.
    use_nse_for_run = True
    if adhoc_urls:
        if not args.bank:
            log.error("--url requires --bank (need to know where downloads go)"); return 2
        if len(banks) != 1:
            log.error("--url needs exactly one matching bank; --bank=%r matched %d",
                      args.bank, len(banks)); return 2
        # If exactly one --type was requested, use it as the type_hint to help
        # the classifier disambiguate. Otherwise stay neutral with "mixed".
        synthetic_hint = "mixed"
        if types_filter and len(types_filter) == 1:
            synthetic_hint = next(iter(types_filter))
        # Copy the bank entry so we don't mutate the loaded config in-memory.
        banks[0] = dict(banks[0])
        banks[0]["sources"] = [
            {"url": u, "type_hint": synthetic_hint, "requires_js": True}
            for u in adhoc_urls
        ]
        # Drop any seed_urls too — --url is "use exactly these sources".
        banks[0].pop("seed_urls", None)
        use_nse_for_run = False
        log.info("Ad-hoc --url override for %s (%d URL%s); NSE skipped",
                 banks[0]["name"], len(adhoc_urls), "" if len(adhoc_urls) == 1 else "s")

    run_summary = {
        "mode": args.mode,
        "started_at": datetime.utcnow().isoformat(timespec='seconds'),
        "history_years": history_years,
        "types_filter": sorted(types_filter) if types_filter else None,
        "adhoc_urls": adhoc_urls or None,
        "banks": [],
    }
    if types_filter:
        log.info("Type filter active: only fetching %s", ", ".join(sorted(types_filter)))
    # Shared mutable state: warm up NSE cookies once per run and reuse for every bank.
    nse_warmed = [False]
    with open_index(db_path) as index:
        for b in banks:
            try:
                stats = process_bank(
                    bank_cfg=b, kb_root=KB_ROOT, fetcher=fetcher, index=index,
                    history_years=history_years, mode=args.mode,
                    use_nse=use_nse_for_run,
                    nse_warmed=nse_warmed,
                    types_filter=types_filter,
                )
                run_summary["banks"].append(stats.as_dict())
            except Exception as e:  # noqa: BLE001 - never let one bank kill the run
                log.exception("Bank %s crashed: %s", b["name"], e)
                run_summary["banks"].append({"bank": b["name"], "fatal_error": str(e)})

        run_summary["index_stats"] = index.stats()

    run_summary["ended_at"] = datetime.utcnow().isoformat(timespec='seconds')
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json"
    log_path.write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    log.info("Run summary -> %s", log_path)

    totals = {k: sum(b.get(k, 0) for b in run_summary["banks"])
              for k in ("discovered", "in_window", "new_downloads",
                        "skipped_existing", "download_failures", "extract_failures")}
    log.info("TOTALS: %s", totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
