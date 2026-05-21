#!/usr/bin/env python3
"""Onboard a new bank (or any tracked entity) to the knowledge base.

This script does two things:

  1. Scaffolds `banks/<Name>/` from `banks/_template/`:
       - config.json   (you fill in ticker + URLs)
       - notes.md      (you'll grow this as you learn the bank's quirks)
       - adapter.py    (NOT copied by default — only needed for tricky banks)
     Then opens config.json in your $EDITOR (unless --no-edit) so you can
     fill in ticker, category, and source URLs immediately.

  2. (Optional, default ON) Runs the 5-year backfill immediately:
     NSE corporate-announcements + IR-page scrape + Playwright (if requested)
     → download → text extract → SQLite index. Pass --no-backfill to skip.

The bank's per-bank `config.json` is the source of truth. There is no
monolithic banks_config.json anymore.

Examples:
    # Interactive: scaffold + open in $EDITOR + backfill
    python3 add_bank.py HDFC_Bank

    # Fully non-interactive
    python3 add_bank.py HDFC_Bank \\
        --ticker HDFCBANK --category private \\
        --source 'https://www.hdfcbank.com/personal/about-us/investor-relations' \\
        --requires-js

    # Scaffold only — don't open editor, don't backfill (good for CI/tests)
    python3 add_bank.py South_Indian_Bank --no-edit --no-backfill \\
        --ticker SOUTHBANK --category private \\
        --source 'https://www.southindianbank.com/investor-relations'

Idempotent: re-running for an existing bank merges new --source entries into
the existing config.json. Use --replace to clobber it instead.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent           # KnowledgeBase/_engine/
KB_ROOT = HERE.parent                             # KnowledgeBase/
BANKS_DIR = HERE / "banks"
TEMPLATE_DIR = BANKS_DIR / "_template"
LOG_DIR = HERE / "_logs"

from bank_kb.structure import SUBFOLDERS, ensure_bank_structure  # noqa: E402


def _slug(name: str) -> str:
    """Folder-safe slug (preserves underscores)."""
    return "".join(ch if ch.isalnum() else "_" for ch in name).strip("_")


# ---------------------------------------------------------------------------
# Scaffold a banks/<Name>/ folder from banks/_template/
# ---------------------------------------------------------------------------

def _scaffold(name: str, replace: bool) -> Path:
    """Create banks/<name>/ from banks/_template/. Returns the folder path.
    Does NOT clobber existing files unless --replace is set.
    """
    target = BANKS_DIR / name
    if target.exists() and not replace:
        # Merge mode: ensure required files exist, don't overwrite anything.
        if not (target / "config.json").exists():
            shutil.copy(TEMPLATE_DIR / "config.json", target / "config.json")
            print(f"  + created missing {target.name}/config.json from template")
        if not (target / "notes.md").exists():
            shutil.copy(TEMPLATE_DIR / "notes.md", target / "notes.md")
            print(f"  + created missing {target.name}/notes.md from template")
        return target
    if target.exists() and replace:
        print(f"  ! --replace: removing existing {target}")
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=False)
    # Copy template files except .example stubs (those are docs, not runtime).
    for src in sorted(TEMPLATE_DIR.iterdir()):
        if src.name.endswith(".example"):
            continue
        shutil.copy(src, target / src.name)
    print(f"  + scaffolded {target} from {TEMPLATE_DIR.name}/")
    return target


def _merge_config(bank_folder: Path, *, name: str, ticker: str | None,
                  category: str | None, sources: list[tuple[str, str]],
                  requires_js: bool) -> dict:
    """Read banks/<X>/config.json, apply CLI overrides, write back. Returns the dict."""
    path = bank_folder / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))

    # Strip template comment keys before writing back (they're informational).
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_")}

    cfg["name"] = name
    if ticker is not None:
        cfg["ticker"] = ticker
    if category:
        cfg["category"] = category
    cfg.setdefault("category", "other")
    cfg.setdefault("ticker", "")

    # Drop the template's placeholder source (example.bank.in) before merging
    # — it exists only so the JSON validates as a non-empty list; it's never
    # a real source.
    existing = [s for s in (cfg.get("sources") or [])
                if isinstance(s, dict) and "example.bank.in" not in (s.get("url") or "")]
    if sources:
        existing_urls = {s.get("url") for s in existing}
        for url, hint in sources:
            if url in existing_urls:
                continue
            entry = {"url": url, "type_hint": hint or "mixed"}
            if requires_js:
                entry["requires_js"] = True
            existing.append(entry)
            existing_urls.add(url)
    cfg["sources"] = existing

    # Normalize doc_types: drop None / placeholder
    if cfg.get("doc_types") in (None, [], "null"):
        cfg.pop("doc_types", None)

    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Optional: open config.json in $EDITOR for the user to finish
# ---------------------------------------------------------------------------

def _open_in_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        print(f"  i  $EDITOR not set — edit this file manually before backfilling:")
        print(f"        {path}")
        return
    print(f"  → opening {path} in {editor}")
    try:
        subprocess.run([editor, str(path)], check=False)
    except Exception as e:  # noqa: BLE001
        print(f"  ! failed to launch {editor}: {e}")
        print(f"    edit manually: {path}")


# ---------------------------------------------------------------------------
# Backfill — invoke the same pipeline run.py uses, in-process, for one bank
# ---------------------------------------------------------------------------

def _run_backfill_for(name: str, verbose: bool = True) -> int:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("add_bank")
    log.info("Starting backfill for %s", name)

    from bank_kb.fetcher import Fetcher
    from bank_kb.indexer import open_index
    from bank_kb.orchestrator import process_bank
    from bank_kb.registry import (adapter_classify_link, adapter_render_page,
                                  load_all_banks, load_settings)

    settings = load_settings(HERE)
    entries = load_all_banks(HERE, only=name)
    if not entries:
        log.error("Bank %r not found after scaffold — aborting backfill", name)
        return 2
    entry = entries[0]

    if not entry.config.get("sources"):
        log.error("Bank %r has no sources configured yet. Edit %s and re-run with:\n"
                  "    python3 run.py --mode backfill --bank %s --verbose",
                  name, entry.folder / "config.json", name)
        return 2

    fetcher = Fetcher(
        user_agent=settings["user_agent"],
        request_delay_seconds=settings["request_delay_seconds"],
        request_timeout_seconds=settings["request_timeout_seconds"],
        max_pdf_size_mb=settings["max_pdf_size_mb"],
    )
    history_years = settings["history_years"]
    doc_types_whitelist = set(settings["doc_types_whitelist"])
    db_path = HERE / "kb_index.sqlite"

    nse_warmed = [False]
    summary = {"mode": "backfill_via_add_bank", "bank": name,
               "started_at": datetime.utcnow().isoformat(timespec="seconds")}
    with open_index(db_path) as index:
        try:
            stats = process_bank(
                bank_cfg=entry.config, kb_root=KB_ROOT, fetcher=fetcher, index=index,
                history_years=history_years, mode="backfill",
                nse_warmed=nse_warmed,
                doc_types_whitelist=doc_types_whitelist,
                render_page_override=adapter_render_page(entry),
                classify_link_override=adapter_classify_link(entry),
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
    print(f"  skipped_by_type: {summary.get('skipped_by_type', 0)}")
    print(f"  failures       : dl={summary.get('download_failures', 0)} "
          f"ex={summary.get('extract_failures', 0)}")
    print(f"  log file       : {log_path}")
    print("=" * 70)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Onboard a new bank/entity (creates banks/<Name>/ from the template).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[-1] if "Examples:" in (__doc__ or "") else None,
    )
    p.add_argument("name",
                   help="Folder name for the bank (e.g. HDFC_Bank). Auto-normalized "
                        "to filesystem-safe characters.")
    p.add_argument("--ticker",
                   help="NSE ticker (e.g. HDFCBANK). Omit for unlisted entities.")
    p.add_argument("--category", default="other",
                   help="Free-form tag (private, public, nbfc, insurance, fintech, ...).")
    p.add_argument("--source", nargs="+", action="append", metavar="URL [TYPE_HINT]",
                   default=[],
                   help="IR page URL, optionally with a type hint "
                        "(investor_presentation|press_release|financial_result|mixed). "
                        "Repeat for multiple sources. Type hint defaults to 'mixed'.")
    p.add_argument("--requires-js", action="store_true",
                   help="Mark all --source pages as needing Playwright rendering.")
    p.add_argument("--replace", action="store_true",
                   help="If banks/<Name>/ already exists, DELETE IT and re-scaffold from template.")
    p.add_argument("--no-edit", action="store_true",
                   help="Don't open config.json in $EDITOR after scaffolding.")
    p.add_argument("--no-backfill", action="store_true",
                   help="Scaffold the bank's folder only; skip the immediate 5-year backfill.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    name = _slug(args.name)
    if name != args.name:
        print(f"Note: normalizing name '{args.name}' -> '{name}' for filesystem safety")

    # Normalize --source values: each --source can be URL or URL TYPE_HINT.
    sources: list[tuple[str, str]] = []
    for s in args.source:
        if len(s) == 1:
            sources.append((s[0], "mixed"))
        elif len(s) == 2:
            sources.append((s[0], s[1]))
        else:
            print(f"ERROR: --source takes 1 or 2 values, got {len(s)}: {s}", file=sys.stderr)
            return 2

    # 1. Scaffold per-bank folder.
    if not TEMPLATE_DIR.exists():
        print(f"ERROR: template folder missing at {TEMPLATE_DIR}. "
              f"Re-clone the repo or restore banks/_template/.", file=sys.stderr)
        return 2

    print(f"Scaffolding {BANKS_DIR}/{name}/")
    bank_folder = _scaffold(name, replace=args.replace)

    # 2. Merge CLI args into config.json.
    cfg = _merge_config(bank_folder, name=name, ticker=args.ticker,
                        category=args.category, sources=sources,
                        requires_js=args.requires_js)
    print(f"  + config.json written: ticker={cfg.get('ticker') or '<none>'}, "
          f"category={cfg.get('category')}, sources={len(cfg.get('sources', []))}")

    # 3. Create the per-bank corpus folders (IP / PR / extracted_text).
    ensure_bank_structure(name, KB_ROOT)
    print(f"  + corpus folders ready: {KB_ROOT / name}/{{{','.join(SUBFOLDERS)}}}")

    # 4. Open in editor unless suppressed.
    if not cfg.get("sources") and not args.no_edit:
        print()
        print(f"No --source URLs were passed and config.json has none either.")
        print(f"Opening {bank_folder / 'config.json'} so you can add at least one source.")
        _open_in_editor(bank_folder / "config.json")
        # Re-load after editor close so the backfill phase sees the user's edits.
        cfg = json.loads((bank_folder / "config.json").read_text(encoding="utf-8"))

    if not cfg.get("ticker"):
        print("  i  No NSE ticker — discovery will rely on the IR sources alone.")

    # 5. Backfill unless suppressed.
    if args.no_backfill:
        print()
        print("Skipped backfill (--no-backfill). Run it later with:")
        print(f"  python3 run.py --mode backfill --bank {name} --verbose")
        return 0
    if not cfg.get("sources"):
        print()
        print("No sources configured — cannot backfill. Edit:")
        print(f"  {bank_folder / 'config.json'}")
        print("then run:")
        print(f"  python3 run.py --mode backfill --bank {name} --verbose")
        return 0

    print()
    print(f"Backfilling {name} now — this may take a few minutes.")
    print("(Pass --no-backfill if you want to register without fetching.)")
    print()
    return _run_backfill_for(name, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
