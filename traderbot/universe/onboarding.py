"""Universe stock onboarding, inspection, and lifecycle management for TraderBot."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional

from traderbot.core_strategy_engine.rollout import get_symbol_rollout_cohort
from traderbot.universe.archetypes import DEFAULT_ARCHETYPES, load_archetypes
from traderbot.universe.sector_resolver import (
    etf_to_sector_name,
    resolve_sector_etf,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config" / "watchers.json"
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.\-]{1,10}$")

ARCHETYPE_DESCRIPTIONS: Dict[str, Dict[str, Any]] = {
    "default": {
        "label": "Default (Balanced)",
        "description": "Standard 5-minute dynamic entry model with 20% max loss, 25% max symbol notional, and 5% trail step.",
        "risk_profile": "large_cap",
        "max_notional_percent": 25,
    },
    "large_cap_tech": {
        "label": "Large Cap Tech / Momentum",
        "description": "Tailored for liquid tech and momentum names with high volume threshold and 25% allocation limit.",
        "risk_profile": "large_cap",
        "max_notional_percent": 25,
    },
    "high_beta_growth": {
        "label": "High Beta Growth",
        "description": "Conservative initial notional ($3,000) and strict 15% symbol cap for volatile or speculative growth names.",
        "risk_profile": "growth",
        "max_notional_percent": 15,
    },
    "defensive_value": {
        "label": "Defensive Value",
        "description": "Tighter 4% trail trigger and 2% structural stop trail for dividend, value, or lower-beta holdings.",
        "risk_profile": "value",
        "max_notional_percent": 20,
    },
}


def iso_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load_json(path: Any, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path: Any, data: Any) -> None:
    """Write JSON atomically so readers and crashes never see a partial file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp_path = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(temp_path, p)
                break
            except PermissionError:
                if attempt == 19:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, sort_keys=True)
                        f.write("\n")
                    break
                time.sleep(0.025)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def get_alpaca_client() -> Optional[Any]:
    """Initialize AlpacaClient safely if credentials are in the environment."""
    try:
        from traderbot.core_strategy_engine.engine import AlpacaClient, load_env
        load_env()
        return AlpacaClient()
    except Exception:
        return None


def validate_symbol_format(symbol: str) -> str:
    """Validate and normalize a stock ticker symbol."""
    if not isinstance(symbol, str):
        raise ValueError("Symbol must be a string.")
    normalized = symbol.upper().strip()
    if not normalized:
        raise ValueError("Symbol cannot be empty.")
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError(f"Invalid symbol format '{normalized}'. Use 1-10 uppercase alphanumeric characters.")
    return normalized


def get_available_archetypes(project_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return available archetypes with descriptions and key risk parameters."""
    root = project_root or ROOT
    loaded = load_archetypes(root)
    result = []
    for key, cfg in loaded.items():
        meta = ARCHETYPE_DESCRIPTIONS.get(key, {
            "label": key.replace("_", " ").title(),
            "description": f"Custom archetype with {cfg.get('risk_profile', 'standard')} profile.",
            "risk_profile": cfg.get("risk_profile", "standard"),
            "max_notional_percent": cfg.get("max_symbol_notional_percent", 20),
        })
        result.append({
            "key": key,
            "label": meta["label"],
            "description": meta["description"],
            "risk_profile": meta.get("risk_profile", cfg.get("risk_profile", "standard")),
            "max_notional_percent": meta.get("max_notional_percent", cfg.get("max_symbol_notional_percent", 20)),
            "dynamic_entry_notional": cfg.get("dynamic_entry_notional", 5000),
            "trail_trigger_step_percent": cfg.get("trail_trigger_step_percent", 5.0),
        })
    return result


def inspect_stock(
    symbol: str,
    project_root: Optional[Path] = None,
    check_broker: bool = True,
) -> Dict[str, Any]:
    """Inspect and validate a stock symbol for onboarding into TraderBot."""
    clean_sym = validate_symbol_format(symbol)
    root = project_root or ROOT
    config_path = root / "config" / "watchers.json"
    config = load_json(config_path, {}) if config_path.exists() else {}

    managed = config.get("managed_watchers", config.get("watchers", []))
    candidates = config.get("new_watchers", [])

    is_managed = any(w.get("symbol") == clean_sym for w in managed)
    is_candidate = any(w.get("symbol") == clean_sym for w in candidates)
    enrolled_group = "managed" if is_managed else "candidate" if is_candidate else None

    sector_etf = resolve_sector_etf(clean_sym, config)
    sector_name = etf_to_sector_name(sector_etf)
    cohort = get_symbol_rollout_cohort(clean_sym, config).upper()

    broker_status = "skipped"
    asset_name = None
    tradable = None
    broker_error = None

    if check_broker:
        client = get_alpaca_client()
        if client:
            try:
                asset = client.asset(clean_sym)
                if asset:
                    tradable = asset.get("tradable", False) and asset.get("status") == "active"
                    asset_name = asset.get("name", "US Equity")
                    broker_status = "active" if tradable else "inactive"
                else:
                    broker_status = "not_found"
                    tradable = False
            except Exception as exc:
                broker_status = "error"
                broker_error = str(exc)

    return {
        "symbol": clean_sym,
        "is_enrolled": enrolled_group is not None,
        "enrolled_group": enrolled_group,
        "sector_etf": sector_etf,
        "sector_name": sector_name,
        "cohort": cohort,
        "broker_status": broker_status,
        "asset_name": asset_name,
        "tradable": tradable,
        "broker_error": broker_error,
        "ready": enrolled_group is None and (tradable is None or tradable is True),
    }


def add_stock_to_universe(
    symbol: str,
    archetype: str = "default",
    group: str = "candidate",
    project_root: Optional[Path] = None,
    skip_validation: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Enroll a stock symbol into the TraderBot universe."""
    clean_sym = validate_symbol_format(symbol)
    root = project_root or ROOT
    config_path = root / "config" / "watchers.json"
    target_group = "new" if group in ("candidate", "new") else "managed"

    available_archetypes = load_archetypes(root)
    if archetype not in available_archetypes:
        raise ValueError(
            f"Unknown archetype '{archetype}'. Available: {list(available_archetypes.keys())}"
        )

    config = load_json(config_path, {}) if config_path.exists() else {}
    managed = config.setdefault("managed_watchers", [])
    candidates = config.setdefault("new_watchers", [])

    if any(w.get("symbol") == clean_sym for w in managed):
        return {
            "success": False,
            "symbol": clean_sym,
            "status": "already_enrolled",
            "enrolled_group": "managed",
            "message": f"Symbol '{clean_sym}' is already enrolled in managed_watchers.",
        }
    if any(w.get("symbol") == clean_sym for w in candidates):
        return {
            "success": False,
            "symbol": clean_sym,
            "status": "already_enrolled",
            "enrolled_group": "candidate",
            "message": f"Symbol '{clean_sym}' is already enrolled in candidate watchers (new_watchers).",
        }

    # Pre-flight check
    inspection = inspect_stock(clean_sym, project_root=root, check_broker=not skip_validation)
    if not skip_validation and inspection.get("broker_status") in ("not_found", "inactive"):
        raise ValueError(
            f"Asset '{clean_sym}' is not tradable or active on Alpaca. Onboarding rejected."
        )

    entry = {"symbol": clean_sym}
    if archetype != "default":
        entry["archetype"] = archetype

    target_list = candidates if target_group == "new" else managed
    target_list.append(entry)

    if not dry_run:
        save_json(config_path, config)

    display_group = "candidates (new_watchers)" if target_group == "new" else "managed_watchers"
    return {
        "success": True,
        "symbol": clean_sym,
        "group": "candidate" if target_group == "new" else "managed",
        "archetype": archetype,
        "sector_etf": inspection["sector_etf"],
        "sector_name": inspection["sector_name"],
        "cohort": inspection["cohort"],
        "message": (
            f"Enrolled '{clean_sym}' into {display_group} with archetype '{archetype}'. "
            "WatcherListReloader will pick up the update on the next scheduler cycle."
        ),
    }


def remove_stock_from_universe(
    symbol: str,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Remove a stock symbol from the TraderBot universe."""
    clean_sym = validate_symbol_format(symbol)
    root = project_root or ROOT
    config_path = root / "config" / "watchers.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found at {config_path}")

    config = load_json(config_path, {})
    managed_before = len(config.get("managed_watchers", []))
    new_before = len(config.get("new_watchers", []))

    config["managed_watchers"] = [
        w for w in config.get("managed_watchers", []) if w.get("symbol") != clean_sym
    ]
    config["new_watchers"] = [
        w for w in config.get("new_watchers", []) if w.get("symbol") != clean_sym
    ]

    managed_removed = managed_before - len(config["managed_watchers"])
    new_removed = new_before - len(config["new_watchers"])

    if managed_removed == 0 and new_removed == 0:
        return {
            "success": False,
            "symbol": clean_sym,
            "status": "not_found",
            "message": f"Symbol '{clean_sym}' was not found in watchers configuration.",
        }

    save_json(config_path, config)
    return {
        "success": True,
        "symbol": clean_sym,
        "managed_removed": managed_removed,
        "candidates_removed": new_removed,
        "message": f"Removed '{clean_sym}' from watchers configuration.",
    }
