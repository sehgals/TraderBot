"""Stage 3: Rollout and Canary Partitioning.

Deterministic A/B hash-split for staged paper rollout.
Partition the symbol universe so 25% of symbols run Stop Mode v2 + Target Mode v2 (Canary)
while 75% run incumbent legacy logic (Control).
"""
import hashlib
from typing import Any, Dict, Optional

DEFAULT_SALT = "stage3_canary"


def get_symbol_rollout_cohort(symbol: str, supervisor_config: Optional[Dict[str, Any]] = None) -> str:
    """Return 'canary' or 'control' deterministically based on symbol hash."""
    if not symbol:
        return "control"

    rollout_cfg = (supervisor_config or {}).get("stage_3_rollout") or {}
    if not rollout_cfg.get("enabled", True):
        return "control"

    sym = symbol.upper()
    explicit_canary = {s.upper() for s in rollout_cfg.get("canary_symbols", [])}
    explicit_control = {s.upper() for s in rollout_cfg.get("control_symbols", [])}

    if sym in explicit_canary:
        return "canary"
    if sym in explicit_control:
        return "control"

    salt = rollout_cfg.get("salt", DEFAULT_SALT)
    h = int(hashlib.md5(f"{salt}:{sym}".encode("utf-8")).hexdigest(), 16)
    canary_fraction = float(rollout_cfg.get("canary_fraction", 0.25))
    divisor = int(round(1.0 / canary_fraction)) if 0 < canary_fraction < 1 else 4
    return "canary" if (h % divisor) == 0 else "control"


def apply_rollout_settings(
    strategy_config: Dict[str, Any],
    supervisor_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Tag strategy_config with rollout_cohort and set stop_mode / target_mode."""
    symbol = strategy_config.get("symbol", "")
    cohort = get_symbol_rollout_cohort(symbol, supervisor_config)
    strategy_config["rollout_cohort"] = cohort

    rollout_cfg = (supervisor_config or {}).get("stage_3_rollout") or {}
    is_active = rollout_cfg.get("enabled", True) and cohort == "canary"

    if is_active:
        strategy_config["stop_mode"] = rollout_cfg.get("stop_mode", "v2")
        strategy_config["target_mode"] = rollout_cfg.get("target_mode", "v2")
        strategy_config["calibrated_structural_stop_percent"] = float(
            rollout_cfg.get("calibrated_structural_stop_percent", 3.5)
        )
        strategy_config["market_ok_hysteresis_bars"] = int(
            rollout_cfg.get("market_ok_hysteresis_bars", 2)
        )
    else:
        strategy_config["stop_mode"] = "legacy"
        strategy_config["target_mode"] = "legacy"
        # In control, maintain standard single-bar regime checks unless explicitly overridden
        if "market_ok_hysteresis_bars" not in strategy_config:
            strategy_config["market_ok_hysteresis_bars"] = 1

    return strategy_config
