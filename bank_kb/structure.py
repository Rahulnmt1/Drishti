"""Per-bank folder skeleton — single source of truth.

Both `add_bank.py` (interactive onboarding) and `run.py` (daily/backfill)
import from here so the structure stays consistent regardless of which entry
point creates a new bank's tree.

If you decide a new doc category should be tracked (e.g. "ESG_reports"), add
it to `SUBFOLDERS` once and every job — `add_bank.py`, `run.py --mode daily`,
`run.py --sync-structure` — will start creating that subfolder for every
bank automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Mapping


SUBFOLDERS: tuple[str, ...] = (
    "investor_presentations",
    "annual_reports",
    "transcripts",
    "press_releases",
    "extracted_text",
)


def ensure_bank_structure(name: str, kb_root: Path) -> bool:
    """Create the per-bank folder tree if it's missing.

    Returns True iff this call created anything new (any subfolder did not
    already exist). Idempotent and cheap to call on every run.
    """
    bank_dir = Path(kb_root) / name
    created_any = False
    for sub in SUBFOLDERS:
        p = bank_dir / sub
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created_any = True
    return created_any


def ensure_all_banks_structure(cfg: Mapping, kb_root: Path) -> List[str]:
    """Walk banks_config.json and ensure folders for every listed bank.

    Returns the list of bank names whose tree was (partially or fully)
    newly created on this call — useful for log lines like
    `Created folder skeleton for 1 new bank(s): South_Indian_Bank`.
    """
    created_for: List[str] = []
    for bank in cfg.get("banks", []):
        name = bank.get("name")
        if not name:
            continue
        if ensure_bank_structure(name, kb_root):
            created_for.append(name)
    return created_for


__all__ = ["SUBFOLDERS", "ensure_bank_structure", "ensure_all_banks_structure"]
