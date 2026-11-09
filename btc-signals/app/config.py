"""Configuration loading: config.yaml for settings, .env for secrets."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Load config.yaml (or BTC_SIGNALS_CONFIG) and secrets from .env."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", encoding="utf-8-sig")   # Notepad may add a BOM
    except ImportError:  # python-dotenv is optional
        pass
    path = Path(path or os.environ.get("BTC_SIGNALS_CONFIG", ROOT / "config.yaml"))
    with open(path, encoding="utf-8-sig") as f:      # -sig: tolerate a Notepad BOM
        cfg = yaml.safe_load(f) or {}
    cfg["_root"] = str(ROOT)
    cfg["secrets"] = {
        "poly_api_key": os.environ.get("POLY_API_KEY", ""),
        "poly_api_secret": os.environ.get("POLY_API_SECRET", ""),
        "poly_api_passphrase": os.environ.get("POLY_API_PASSPHRASE", ""),
    }
    return cfg


def resolve_path(cfg: dict, p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else Path(cfg.get("_root", ROOT)) / path
