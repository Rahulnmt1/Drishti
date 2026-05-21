"""Per-bank folder skeleton — single source of truth.

Both `add_bank.py` (interactive onboarding) and `run.py` (daily/backfill)
import from here so the structure stays consistent regardless of which entry
point creates a new bank's tree.

If you decide a new doc category should be tracked (e.g. "ESG_reports"), add
it to `SUBFOLDERS` once and every job — `add_bank.py`, `run.py --mode daily`,
`run.py --sync-structure` — will start creating that subfolder for every
bank automatically.

The current shape is intentionally minimal: only the doc types listed in
`settings.json:doc_types_whitelist` are fetched, and only IPs and PRs land
in their own folder. `financial_result` PDFs are aliased to
`investor_presentations/` by `orchestrator.TYPE_TO_FOLDER`.
"""

from __future__ import annotations

from pathlib import Path


SUBFOLDERS: tuple[str, ...] = (
    "investor_presentations",
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


__all__ = ["SUBFOLDERS", "ensure_bank_structure"]
