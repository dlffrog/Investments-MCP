"""
config.py — Load server configuration.

Priority:
  1. config.local.yaml in repo root (gitignored, contains secrets)
  2. {VAULT_ROOT}/Scripts/config.yaml — the existing vault config (fallback)
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
LOCAL_CONFIG = REPO_ROOT / "config.local.yaml"


def load_config() -> dict:
    """Return config dict. Local config takes full precedence."""
    if LOCAL_CONFIG.exists():
        with open(LOCAL_CONFIG) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg

    # Fallback: locate via VAULT_ROOT env var
    vault_root = os.environ.get("VAULT_ROOT")
    if vault_root:
        vault_config = Path(vault_root) / "Scripts" / "config.yaml"
        if vault_config.exists():
            with open(vault_config) as f:
                return yaml.safe_load(f) or {}

    raise FileNotFoundError(
        "No config found. Copy config.example.yaml → config.local.yaml "
        "and fill in vault path, EODHD key, and auth token."
    )


def save_fx_cache(rates: dict[str, float]) -> None:
    """Write fresh FX rates back to config.local.yaml as fallback cache."""
    if not LOCAL_CONFIG.exists():
        return  # Never write to the vault's config.yaml from here
    with open(LOCAL_CONFIG) as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("fx_rates", {}).update({k: round(v, 6) for k, v in rates.items()})
    with open(LOCAL_CONFIG, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
