"""Bank registry — loads global settings + per-bank configs + per-bank adapters.

This is the new home for "what entities does the engine track and how?". It
replaces the old monolithic `banks_config.json` with a per-bank workspace
under `_engine/banks/<Bank>/`. See `_engine/banks/README.md` for the on-disk
layout.

Three responsibilities:
    1. Load global settings (`_engine/settings.json`).
    2. Scan `_engine/banks/*/config.json`, validate, return a list of dicts
       in the same shape `orchestrator.process_bank` already consumes.
    3. Lazy-import an optional per-bank `adapter.py` and expose its hooks
       (`render_page`, `classify_link`) — or noop wrappers if absent.

Adapters give each bank an escape hatch for site-specific quirks (custom
React year-pickers, etc.) without polluting the engine code with per-bank
branching.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# Names reserved at the top of `banks/` that are NOT banks.
_RESERVED_NAMES = {"_template", "README.md", ".DS_Store"}

# Engine-wide doc-type whitelist if settings.json doesn't override it.
_DEFAULT_DOC_TYPES_WHITELIST = (
    "investor_presentation",
    "press_release",
    "financial_result",
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings(engine_dir: Path) -> dict:
    """Read `_engine/settings.json`. Tolerates the file being absent or
    containing comment-keys starting with `_`.
    """
    path = engine_dir / "settings.json"
    if not path.exists():
        log.warning("settings.json missing at %s — using built-in defaults", path)
        return _settings_defaults()
    raw = json.loads(path.read_text(encoding="utf-8"))
    clean = {k: v for k, v in raw.items() if not k.startswith("_")}
    out = {**_settings_defaults(), **clean}
    if not isinstance(out.get("doc_types_whitelist"), (list, tuple)):
        out["doc_types_whitelist"] = list(_DEFAULT_DOC_TYPES_WHITELIST)
    return out


def _settings_defaults() -> dict:
    return {
        "history_years": 5,
        "user_agent": "Mozilla/5.0 BankKBBot/1.0",
        "request_delay_seconds": 2.0,
        "request_timeout_seconds": 45,
        "max_pdf_size_mb": 80,
        "doc_types_whitelist": list(_DEFAULT_DOC_TYPES_WHITELIST),
    }


# ---------------------------------------------------------------------------
# Banks
# ---------------------------------------------------------------------------

@dataclass
class BankEntry:
    """In-memory view of one `banks/<X>/` folder.

    `config` matches the shape `orchestrator.process_bank` already consumes
    (with `name`, `ticker`, `category`, `sources` keys) — so we can drop this
    into the existing pipeline with zero shim code.
    `adapter` is the loaded Python module (or None if the bank doesn't have one).
    `folder` is the absolute path to the per-bank workspace.
    """
    config: dict
    adapter: Optional[ModuleType]
    folder: Path

    @property
    def name(self) -> str:
        return self.config["name"]


def load_all_banks(engine_dir: Path,
                   *,
                   only: Optional[str] = None) -> list[BankEntry]:
    """Walk `engine_dir/banks/*/config.json` and return one BankEntry per bank.

    `only` filters to a single bank name (case-insensitive). When set and the
    bank doesn't exist on disk, raises SystemExit with a helpful message
    listing what IS registered — easier to diagnose than a silent empty list.
    """
    banks_dir = engine_dir / "banks"
    if not banks_dir.exists():
        raise SystemExit(
            f"banks/ directory missing at {banks_dir}.\n"
            f"Run `mkdir -p {banks_dir}/_template` and add banks with "
            f"`python3 add_bank.py <name>`."
        )

    entries: list[BankEntry] = []
    for child in sorted(banks_dir.iterdir()):
        if child.name in _RESERVED_NAMES or not child.is_dir():
            continue
        cfg_path = child / "config.json"
        if not cfg_path.exists():
            log.warning("Skipping %s — no config.json", child)
            continue
        try:
            cfg = _load_bank_config(cfg_path)
        except (json.JSONDecodeError, ValueError) as e:
            log.error("Invalid config.json in %s: %s — skipping", child, e)
            continue
        if cfg["name"] != child.name:
            log.warning(
                "Folder name %r doesn't match config['name']=%r — using folder name",
                child.name, cfg["name"],
            )
            cfg["name"] = child.name
        adapter = _load_adapter(child)
        entries.append(BankEntry(config=cfg, adapter=adapter, folder=child))

    if only:
        wanted = only.lower()
        entries = [e for e in entries if e.name.lower() == wanted]
        if not entries:
            registered = [p.name for p in sorted(banks_dir.iterdir())
                          if p.is_dir() and p.name not in _RESERVED_NAMES]
            raise SystemExit(
                f"No bank named {only!r} under {banks_dir}/.\n"
                f"Registered banks: {registered or '<none>'}\n"
                f"Add a new bank with: python3 add_bank.py {only}"
            )
    return entries


def _load_bank_config(path: Path) -> dict:
    """Parse and validate a single banks/<X>/config.json."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = {k: v for k, v in raw.items() if not k.startswith("_")}
    if not cfg.get("name"):
        raise ValueError("missing 'name'")
    if not cfg.get("sources"):
        raise ValueError("'sources' must be a non-empty list")
    if not isinstance(cfg["sources"], list):
        raise ValueError("'sources' must be a list")
    cfg.setdefault("category", "other")
    cfg.setdefault("ticker", "")
    # Per-bank doc_types may be present but null (template default) — treat
    # as "inherit global". An explicit list narrows the global whitelist.
    if cfg.get("doc_types") is None:
        cfg.pop("doc_types", None)
    # use_nse defaults to True; banks whose IR page is curated + exhaustive
    # (e.g. HDFC) should set this to false to avoid NSE pulling in duplicate
    # / off-topic filings that the per-bank adapter intentionally excluded.
    if "use_nse" not in cfg:
        cfg["use_nse"] = True
    return cfg


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

def _load_adapter(bank_folder: Path) -> Optional[ModuleType]:
    """Import banks/<X>/adapter.py as an isolated module, if it exists.

    We deliberately use importlib.util to give every adapter its own module
    namespace (`bank_kb._adapters.<bank_slug>`), so two banks with adapters
    can't accidentally clobber each other's module-level state.
    """
    path = bank_folder / "adapter.py"
    if not path.exists():
        return None
    mod_name = f"bank_kb._adapters.{bank_folder.name.lower()}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        log.warning("Couldn't build import spec for adapter %s", path)
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 — never let a broken adapter kill the run
        log.exception("Loading adapter %s failed: %s", path, e)
        return None
    log.info("Loaded adapter for %s (hooks: %s)", bank_folder.name,
             ", ".join(sorted(_adapter_hooks(mod))) or "<none>")
    return mod


def _adapter_hooks(mod: Optional[ModuleType]) -> set[str]:
    """Names of hook functions actually defined on this adapter module."""
    if mod is None:
        return set()
    candidates = {"render_page", "classify_link"}
    return {h for h in candidates if callable(getattr(mod, h, None))}


def adapter_render_page(entry: BankEntry) -> Optional[Callable]:
    """Return the bank's render_page hook, or None to use the generic renderer."""
    if entry.adapter and callable(getattr(entry.adapter, "render_page", None)):
        return entry.adapter.render_page
    return None


def adapter_classify_link(entry: BankEntry) -> Optional[Callable]:
    """Return the bank's classify_link hook, or None to use the generic classifier."""
    if entry.adapter and callable(getattr(entry.adapter, "classify_link", None)):
        return entry.adapter.classify_link
    return None
