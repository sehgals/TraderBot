"""Strategy archetypes and convention-over-configuration resolution for TraderBot.

Provides default risk/strategy archetypes and automatically resolves state, log,
and configuration paths so adding new symbols requires only a ticker symbol.
"""

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import json

STRATEGY_CONFIG_DIR = Path("traderbot") / "core_strategy_engine" / "strategies" / "configs"
RUNTIME_STATE_DIR = Path("runtime") / "state"
RUNTIME_LOG_DIR = Path("runtime") / "logs"


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def resolve_path(project_root, path):
    candidate = Path(path)
    return candidate if candidate.is_absolute() else project_root / candidate

DEFAULT_ARCHETYPES: Dict[str, Dict[str, Any]] = {
    "default": {
        "dynamic_entry_enabled": False,
        "dynamic_entry_notional": 5000,
        "dynamic_entry_time_in_force": "day",
        "dynamic_lookback_days": 7,
        "dynamic_market_filter_ignored_notional": 2500,
        "dynamic_reentry_enabled": True,
        "dynamic_replace_min_change_percent": 0.25,
        "dynamic_replace_open_orders": True,
        "dynamic_timeframe": "5Min",
        "entry_quantity": 1,
        "initial_stop_loss_percent": 20,
        "ladder_buy_quantity": 0,
        "ladder_drop_steps_percent": [],
        "market_closed_poll_seconds": 900,
        "max_symbol_notional_percent": 25,
        "min_cash_balance_percent": 20,
        "poll_seconds": 60,
        "reentry_cooldown_seconds": 600,
        "reentry_enabled": True,
        "reentry_max_per_day": 1,
        "reentry_quantity": 1,
        "risk_profile": "large_cap",
        "trail_stop_below_current_percent": 2.5,
        "trail_trigger_step_percent": 5,
    },
    "large_cap_tech": {
        "dynamic_entry_enabled": False,
        "dynamic_entry_notional": 5000,
        "dynamic_entry_time_in_force": "day",
        "dynamic_lookback_days": 7,
        "dynamic_market_filter_ignored_notional": 2500,
        "dynamic_reentry_enabled": True,
        "dynamic_replace_min_change_percent": 0.25,
        "dynamic_replace_open_orders": True,
        "dynamic_timeframe": "5Min",
        "entry_quantity": 1,
        "initial_stop_loss_percent": 20,
        "ladder_buy_quantity": 0,
        "ladder_drop_steps_percent": [],
        "market_closed_poll_seconds": 900,
        "max_symbol_notional_percent": 25,
        "min_cash_balance_percent": 20,
        "poll_seconds": 60,
        "reentry_cooldown_seconds": 600,
        "reentry_enabled": True,
        "reentry_max_per_day": 1,
        "reentry_quantity": 1,
        "risk_profile": "large_cap",
        "trail_stop_below_current_percent": 2.5,
        "trail_trigger_step_percent": 5.0,
    },
    "high_beta_growth": {
        "dynamic_entry_enabled": False,
        "dynamic_entry_notional": 3000,
        "dynamic_entry_time_in_force": "day",
        "dynamic_lookback_days": 7,
        "dynamic_market_filter_ignored_notional": 1500,
        "dynamic_reentry_enabled": True,
        "dynamic_replace_min_change_percent": 0.25,
        "dynamic_replace_open_orders": True,
        "dynamic_timeframe": "5Min",
        "entry_quantity": 1,
        "initial_stop_loss_percent": 20,
        "ladder_buy_quantity": 0,
        "ladder_drop_steps_percent": [],
        "market_closed_poll_seconds": 900,
        "max_symbol_notional_percent": 15,
        "min_cash_balance_percent": 20,
        "poll_seconds": 60,
        "reentry_cooldown_seconds": 600,
        "reentry_enabled": True,
        "reentry_max_per_day": 1,
        "reentry_quantity": 1,
        "risk_profile": "high_beta",
        "trail_stop_below_current_percent": 4.0,
        "trail_trigger_step_percent": 7.0,
    },
    "defensive_value": {
        "dynamic_entry_enabled": False,
        "dynamic_entry_notional": 5000,
        "dynamic_entry_time_in_force": "day",
        "dynamic_lookback_days": 7,
        "dynamic_market_filter_ignored_notional": 2500,
        "dynamic_reentry_enabled": True,
        "dynamic_replace_min_change_percent": 0.25,
        "dynamic_replace_open_orders": True,
        "dynamic_timeframe": "5Min",
        "entry_quantity": 1,
        "initial_stop_loss_percent": 20,
        "ladder_buy_quantity": 0,
        "ladder_drop_steps_percent": [],
        "market_closed_poll_seconds": 900,
        "max_symbol_notional_percent": 20,
        "min_cash_balance_percent": 20,
        "poll_seconds": 60,
        "reentry_cooldown_seconds": 600,
        "reentry_enabled": True,
        "reentry_max_per_day": 1,
        "reentry_quantity": 1,
        "risk_profile": "value",
        "trail_stop_below_current_percent": 2.0,
        "trail_trigger_step_percent": 4.0,
    },
}


def load_archetypes(project_root: Optional[Path] = None, supervisor_config: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Load archetypes by merging defaults, external config/archetypes.json, and supervisor_config."""
    archetypes = {name: dict(cfg) for name, cfg in DEFAULT_ARCHETYPES.items()}
    if project_root:
        file_path = project_root / "config" / "archetypes.json"
        if file_path.exists():
            file_archetypes = load_json(file_path, {}).get("archetypes", {})
            for name, cfg in file_archetypes.items():
                if name in archetypes:
                    archetypes[name].update(cfg)
                else:
                    archetypes[name] = dict(cfg)

    if supervisor_config and "archetypes" in supervisor_config:
        for name, cfg in supervisor_config.get("archetypes", {}).items():
            if name in archetypes:
                archetypes[name].update(cfg)
            else:
                archetypes[name] = dict(cfg)

    return archetypes


def resolve_watcher_paths(
    symbol: str,
    group: str = "managed",
    project_root: Optional[Path] = None,
    config_file: Optional[str] = None,
    state_file: Optional[str] = None,
    log_file: Optional[str] = None,
) -> Tuple[Optional[Path], Path, Path]:
    """Resolve configuration, state, and log paths by convention if omitted.
    
    Returns (config_path, state_path, log_path).
    """
    root = project_root or Path.cwd()
    lower_sym = symbol.lower()

    # 1. Config path
    if config_file:
        resolved_config = resolve_path(root, config_file)
    else:
        resolved_config = None

    # 2. State path convention
    if state_file:
        resolved_state = resolve_path(root, state_file)
    else:
        if group == "new":
            resolved_state = root / "runtime" / "state" / "candidates" / f"{lower_sym}_strategy_state.json"
        else:
            resolved_state = root / "runtime" / "state" / f"{lower_sym}_strategy_state.json"

    # 3. Log path convention
    if log_file:
        resolved_log = resolve_path(root, log_file)
    else:
        if group == "new":
            resolved_log = root / "runtime" / "logs" / "candidates" / f"{lower_sym}_watcher.jsonl"
        else:
            resolved_log = root / "runtime" / "logs" / f"{lower_sym}_watcher.jsonl"

    return resolved_config, resolved_state, resolved_log


def resolve_watcher_strategy_defaults(
    symbol: str,
    watcher: Dict[str, Any],
    supervisor_config: Dict[str, Any],
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Merge archetype defaults, group watcher defaults, and per-watcher config defaults."""
    archetypes = load_archetypes(project_root, supervisor_config)
    archetype_name = watcher.get("archetype", "default")
    base = dict(archetypes.get(archetype_name, archetypes["default"]))

    group = watcher.get("group", "managed")
    defaults_key = f"{group}_watcher_defaults"
    group_defaults = supervisor_config.get(defaults_key, {})
    watcher_defaults = watcher.get("config_defaults", {})

    merged = {
        **base,
        **group_defaults,
        **watcher_defaults,
    }
    merged.setdefault("symbol", symbol)
    if group == "new":
        merged.setdefault("strategy_type", "new_candidate_dynamic_entry")
        merged.setdefault("strategy_name", f"New candidate dynamic entry for {symbol}")
        merged["dynamic_entry_enabled"] = True
    else:
        merged.setdefault("strategy_type", "managed_dynamic_reentry")
        merged.setdefault("strategy_name", f"Managed dynamic reentry for {symbol}")

    return merged
