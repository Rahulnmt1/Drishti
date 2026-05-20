#!/usr/bin/env python3
"""Search the knowledge base from the command line.

Examples:
    # Top investor-presentation hits across all banks for "generative AI":
    python query.py "generative AI" --type investor_presentation

    # HDFC-only, last FY:
    python query.py "co-lending OR account aggregator" --bank HDFC_Bank --fy 2025

    # Anything tagged with the digital_banking topic:
    python query.py "" --topic digital_banking --limit 50

    # Show corpus stats:
    python query.py --stats
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bank_kb.indexer import open_index

HERE = Path(__file__).resolve().parent
DB_PATH = HERE.parent / "_engine" / "kb_index.sqlite"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Search the banking knowledge base.")
    p.add_argument("query", nargs="?", default="", help="FTS5 query string")
    p.add_argument("--bank", help="Bank name from banks_config.json (e.g. HDFC_Bank)")
    p.add_argument("--type", dest="doc_type",
                   choices=("investor_presentation", "annual_report", "transcript",
                            "press_release", "financial_result", "other"))
    p.add_argument("--topic", help="Topic flag (ai_ml, digital_banking, channels_integration, ...)")
    p.add_argument("--fy", type=int, help="Fiscal year, e.g. 2025 (FY25 = Apr 2024–Mar 2025)")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--raw", action="store_true",
                   help="Pass the query to FTS5 verbatim (boolean operators, NEAR, etc).")
    p.add_argument("--json", action="store_true", help="Emit results as JSON")
    p.add_argument("--stats", action="store_true", help="Print corpus stats and exit")
    args = p.parse_args(argv)

    if not DB_PATH.exists():
        print(f"No index found at {DB_PATH}. Run `python run.py --mode backfill` first.",
              file=sys.stderr)
        return 2

    with open_index(DB_PATH) as idx:
        if args.stats:
            print(json.dumps(idx.stats(), indent=2)); return 0
        if not args.query and not args.topic:
            print("Need a query string OR --topic (or --stats).", file=sys.stderr); return 2
        # FTS5 requires non-empty MATCH; use a wildcard if topic-only filter is requested.
        q = args.query.strip() or "bank OR strategy OR digital OR customer"
        results = idx.search(q, bank=args.bank, doc_type=args.doc_type,
                             topic=args.topic, fy=args.fy, limit=args.limit,
                             raw=args.raw)

    if args.json:
        print(json.dumps(results, indent=2)); return 0
    if not results:
        print("(no matches)"); return 0
    for r in results:
        q_str = f" Q{r['fiscal_quarter']}" if r['fiscal_quarter'] else ""
        fy_str = f" FY{r['fiscal_year']}" if r['fiscal_year'] else ""
        print(f"[{r['bank']}{fy_str}{q_str}] [{r['doc_type']}] {r['title']}")
        print(f"  file:   {r['file_path']}")
        print(f"  source: {r['source_url']}")
        topics = json.loads(r['topic_hits'] or "{}")
        if topics:
            print(f"  topics: {', '.join(f'{k}({v})' for k, v in sorted(topics.items(), key=lambda x: -x[1])[:6])}")
        if r.get('snippet'):
            print(f"  snippet: {r['snippet'][:400]}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
