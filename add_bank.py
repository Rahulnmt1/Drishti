#!/usr/bin/env python3
"""Add a new bank (or any tracked customer / entity) to the knowledge base.

By default this script does everything in one shot:
  1. Creates the folder structure under KnowledgeBase/<Name>/.
  2. Appends/merges an entry in banks_config.json.
  3. Immediately runs the full 5-year backfill — NSE corporate-announcements +
     IR-page scrape + Playwright (if needed) → download → text extract → index.

So a single command both onboards the customer and downloads all their data.
Pass --no-backfill if you want to register the entry without fetching yet
(useful when you're configuring offline or testing the script).

Use cases:

    # New private-sector bank, NSE-listed — pulls 5 years of filings:
    python3 add_bank.py --name South_Indian_Bank --ticker SOUTHBANK \
        --category private \
        --source "https://www.southindianbank.com/investor-relations" mixed

    # NBFC not on NSE — IR-page scraping only:
    python3 add_bank.py --name Bajaj_Finance --category nbfc \
        --source "https://www.bajajfinserv.in/investor-relations" mixed

    # Insurance customer, NSE-listed, with multiple IR landing pages (JS-rendered):
    python3 add_bank.py --name HDFC_Life --ticker HDFCLIFE --category insurance \
        --source "https://www.hdfclife.com/investor-relations/financials" mixed \
        --source "https://www.hdfclife.com/investor-relations/annual-reports" annual_report \
        --requires-js

    # Register-only, don't backfill yet:
    python3 add_bank.py --name X_Bank --ticker XBANK --category private \
        --source "https://www.x.com/ir" mixed --no-backfill

Idempotent: re-running with the same name merges new sources into the existing
entry. Use --replace to clobber it instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent           # KnowledgeBase/_engine/
KB_ROOT = HERE.parent                             # KnowledgeBase/
CONFIG_PATH = HERE / "banks_config.json"
LOG_DIR = HERE / "_logs"                          # KnowledgeBase/_engine/_logs/

# bank_kb.structure is the single source of truth for the per-bank folder
# skeleton. Both add_bank.py and run.py go through it, so changing SUBFOLDERS
# in one place propagates everywhere.
from bank_kb.structure import SUBFOLDERS, ensure_bank_structure  # noqa: E402


def _slug(name: str) -> str:
    """Replace spaces and unfriendly chars with underscores."""
    return "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def add_bank(*, name: str, ticker: str | None, category: str,
             sources: list[tuple[str, str]], requires_js: bool,
             seed_urls: list[str], replace: bool) -> int:
    """Return 0 on success, non-zero on error."""
    safe_name = _slug(name)
    if safe_name != name:
        print(f"Note: normalizing name '{name}' -> '{safe_name}' for filesystem safety")
        name = safe_name

    # 1. Create folder structure (idempotent — shared with run.py).
    bank_dir = KB_ROOT / name
    ensure_bank_structure(name, KB_ROOT)
    print(f"✓ Folders ready: {bank_dir}/{{{','.join(SUBFOLDERS)}}}")

    # 2. Update config.
    cfg = _load_config()
    existing_idx = next((i for i, b in enumerate(cfg["banks"]) if b["name"] == name), None)
    entry: dict = {
        "name": name,
        "ticker": ticker or "",
        "category": category,
        "sources": [
            {"url": url, "type_hint": hint, **({"requires_js": True} if requires_js else {})}
            for url, hint in sources
        ],
    }
    if seed_urls:
        entry["seed_urls"] = [{"url": u, "type_hint": "mixed"} for u in seed_urls]

    if existing_idx is not None:
        if not replace:
            # Merge: union sources & seed_urls, keep existing fields if not provided.
            old = cfg["banks"][existing_idx]
            old_source_urls = {s["url"] for s in old.get("sources", [])}
            for s in entry["sources"]:
                if s["url"] not in old_source_urls:
                    old.setdefault("sources", []).append(s)
            for s in entry.get("seed_urls", []):
                if s["url"] not in {x["url"] for x in old.get("seed_urls", [])}:
                    old.setdefault("seed_urls", []).append(s)
            if ticker:
                old["ticker"] = ticker
            if category and not old.get("category"):
                old["category"] = category
            cfg["banks"][existing_idx] = old
            print(f"✓ Merged into existing config entry for '{name}'")
        else:
            cfg["banks"][existing_idx] = entry
            print(f"✓ Replaced existing config entry for '{name}'")
    else:
        cfg["banks"].append(entry)
        print(f"✓ Added new config entry for '{name}'")

    _save_config(cfg)

    if ticker:
        print(f"  NSE filings will be pulled automatically using ticker '{ticker}'.")
    else:
        print("  No NSE ticker provided — discovery will rely on the IR sources alone.")
    return 0


def _run_backfill_for(name: str, verbose: bool = True) -> int:
    """Invoke the same pipeline run.py would, for a single bank, in-process.

    Imported lazily so `add_bank.py --no-backfill` works even when the engine's
    runtime deps aren't installed yet.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("add_bank")
    log.info("Starting backfill for %s", name)

    from datetime import datetime
    from bank_kb.fetcher import Fetcher
    from bank_kb.indexer import open_index
    from bank_kb.orchestrator import load_config, process_bank

    cfg = load_config(CONFIG_PATH)
    settings = cfg.get("settings", {})
    bank_cfg = next((b for b in cfg["banks"] if b["name"] == name), None)
    if not bank_cfg:
        log.error("Bank %r missing from config after add — aborting backfill", name)
        return 2

    fetcher = Fetcher(
        user_agent=settings.get("user_agent", "BankKBBot/1.0"),
        request_delay_seconds=settings.get("request_delay_seconds", 2.0),
        request_timeout_seconds=settings.get("request_timeout_seconds", 45),
        max_pdf_size_mb=settings.get("max_pdf_size_mb", 80),
    )
    history_years = settings.get("history_years", 5)
    db_path = KB_ROOT / "_engine" / "kb_index.sqlite"

    nse_warmed = [False]
    summary = {"mode": "backfill_via_add_bank", "bank": name,
               "started_at": datetime.utcnow().isoformat(timespec="seconds")}
    with open_index(db_path) as index:
        try:
            stats = process_bank(
                bank_cfg=bank_cfg, kb_root=KB_ROOT, fetcher=fetcher, index=index,
                history_years=history_years, mode="backfill",
                nse_warmed=nse_warmed,
            )
            summary.update(stats.as_dict())
        except Exception as e:  # noqa: BLE001
            log.exception("Backfill for %s failed: %s", name, e)
            summary["fatal_error"] = str(e)
            return 3
        summary["index_stats"] = index.stats()
    summary["ended_at"] = datetime.utcnow().isoformat(timespec="seconds")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y-%m-%d_%H%M')}_addbank_{name}.json"
    log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print()
    print("=" * 70)
    print(f"Backfill complete for {name}:")
    print(f"  discovered     : {summary.get('discovered', 0)} "
          f"(nse={summary.get('discovered_via_nse', 0)}, ir={summary.get('discovered_via_ir', 0)})")
    print(f"  in 5y window   : {summary.get('in_window', 0)}")
    print(f"  downloaded     : {summary.get('new_downloads', 0)}")
    print(f"  skipped (seen) : {summary.get('skipped_existing', 0)}")
    print(f"  failures       : dl={summary.get('download_failures', 0)} "
          f"ex={summary.get('extract_failures', 0)}")
    print(f"  log file       : {log_path}")
    print("=" * 70)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Onboard a new bank/customer to the KB.")
    p.add_argument("--name", required=True,
                   help="Folder/config name. Spaces and slashes auto-normalized to '_'.")
    p.add_argument("--ticker",
                   help="NSE ticker (e.g. SOUTHBANK). Omit for unlisted entities.")
    p.add_argument("--category", default="other",
                   help="Free-form tag (private, public, nbfc, insurance, fintech, ...).")
    p.add_argument("--source", nargs=2, action="append", metavar=("URL", "TYPE_HINT"),
                   default=[],
                   help="IR/press page URL + type hint "
                        "(investor_presentation|annual_report|transcript|press_release|mixed). "
                        "Repeat for multiple sources.")
    p.add_argument("--requires-js", action="store_true",
                   help="Mark all --source pages as needing Playwright rendering.")
    p.add_argument("--seed-url", action="append", default=[],
                   help="Direct PDF URL to seed-import (rare; for one-off backfills).")
    p.add_argument("--replace", action="store_true",
                   help="If the bank already exists, replace it instead of merging.")
    p.add_argument("--no-backfill", action="store_true",
                   help="Register the entry only; skip the immediate 5-year backfill.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose logging during the backfill phase.")
    args = p.parse_args(argv)

    if not args.source and not args.ticker:
        print("ERROR: Provide at least one --source URL or a --ticker for NSE discovery.",
              file=sys.stderr)
        return 2

    rc = add_bank(
        name=args.name, ticker=args.ticker, category=args.category,
        sources=args.source, requires_js=args.requires_js,
        seed_urls=args.seed_url, replace=args.replace,
    )
    if rc != 0:
        return rc

    if args.no_backfill:
        print()
        print("Skipped backfill (--no-backfill). Run it later with:")
        print(f"  python3 run.py --mode backfill --bank {_slug(args.name)} --verbose")
        return 0

    # Default behaviour: download everything for this new customer right now.
    print()
    print(f"Backfilling {_slug(args.name)} now — this may take a few minutes.")
    print("(Pass --no-backfill if you want to register without fetching.)")
    print()
    return _run_backfill_for(_slug(args.name), verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
