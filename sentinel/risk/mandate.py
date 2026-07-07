"""Risk mandate loading and helper predicates."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sentinel.config.settings import load_mandate as _load_config_mandate
from sentinel.core.models import Mandate
from sentinel.risk.audit import append_audit


def mandate_path(root: Path | None = None) -> Path:
    """Return the mandate.toml path for a project root."""

    return (root or Path.cwd()) / "mandate.toml"


def mandate_hash(root: Path | None = None) -> str:
    """Return a SHA-256 content hash for mandate.toml, or an empty-file hash."""

    path = mandate_path(root)
    data = path.read_bytes() if path.exists() else b""
    return hashlib.sha256(data).hexdigest()


def load_validated_mandate(root: Path | None = None, *, actor: str = "system") -> Mandate:
    """Load the configured mandate and record the content hash to the audit ledger."""

    mandate = _load_config_mandate(root)
    append_audit(
        "mandate_loaded",
        {
            "path": str(mandate_path(root)),
            "sha256": mandate_hash(root),
            "symbol_count": len(mandate.symbol_universe),
        },
        actor=actor,
    )
    return mandate


def is_symbol_allowed(symbol: str, mandate: Mandate) -> bool:
    """Return whether a symbol is in the mandate universe."""

    return symbol.upper() in {allowed.upper() for allowed in mandate.symbol_universe}
