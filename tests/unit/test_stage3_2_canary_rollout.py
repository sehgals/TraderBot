"""Unit tests for Stage 3.2: 25% Canary Rollout (A/B Hash-Split)."""
import json
from pathlib import Path

from traderbot.core_strategy_engine.rollout import (
    get_symbol_rollout_cohort,
    apply_rollout_settings,
)
from traderbot.core_strategy_engine.engine import (
    managed_initial_floor_price,
    dynamic_entry_plan,
    ensure_position_episode,
)


def test_canary_cohort_deterministic_hash_split():
    # Load symbols from watchers.json
    watchers_cfg = json.loads(Path("config/watchers.json").read_text(encoding="utf-8"))
    symbols = [w["symbol"] for w in watchers_cfg.get("managed_watchers", [])]
    assert len(symbols) >= 50

    canary = [s for s in symbols if get_symbol_rollout_cohort(s, watchers_cfg) == "canary"]
    control = [s for s in symbols if get_symbol_rollout_cohort(s, watchers_cfg) == "control"]

    assert len(canary) + len(control) == len(symbols)
    # Check that canary fraction is ~25% (between 20% and 30%)
    fraction = len(canary) / len(symbols)
    assert 0.20 <= fraction <= 0.30

    # Idempotent: same symbol always returns the exact same cohort
    for s in symbols:
        assert get_symbol_rollout_cohort(s, watchers_cfg) == get_symbol_rollout_cohort(s, watchers_cfg)


def test_canary_explicit_overrides():
    custom_cfg = {
        "stage_3_rollout": {
            "enabled": True,
            "canary_symbols": ["OVERRIDE_CANARY"],
            "control_symbols": ["OVERRIDE_CONTROL"],
        }
    }
    assert get_symbol_rollout_cohort("OVERRIDE_CANARY", custom_cfg) == "canary"
    assert get_symbol_rollout_cohort("OVERRIDE_CONTROL", custom_cfg) == "control"

    # When rollout is disabled, all fall back to control
    disabled_cfg = {"stage_3_rollout": {"enabled": False}}
    assert get_symbol_rollout_cohort("CRM", disabled_cfg) == "control"


def test_apply_rollout_settings():
    supervisor_cfg = {
        "stage_3_rollout": {
            "enabled": True,
            "canary_symbols": ["CANARY_TEST"],
            "control_symbols": ["CONTROL_TEST"],
            "stop_mode": "v2",
            "target_mode": "v2",
            "calibrated_structural_stop_percent": 3.5,
            "market_ok_hysteresis_bars": 2,
        }
    }

    canary_cfg = {"symbol": "CANARY_TEST"}
    applied_canary = apply_rollout_settings(canary_cfg, supervisor_cfg)
    assert applied_canary["rollout_cohort"] == "canary"
    assert applied_canary["stop_mode"] == "v2"
    assert applied_canary["target_mode"] == "v2"
    assert applied_canary["calibrated_structural_stop_percent"] == 3.5
    assert applied_canary["market_ok_hysteresis_bars"] == 2

    control_cfg = {"symbol": "CONTROL_TEST"}
    applied_control = apply_rollout_settings(control_cfg, supervisor_cfg)
    assert applied_control["rollout_cohort"] == "control"
    assert applied_control["stop_mode"] == "legacy"
    assert applied_control["target_mode"] == "legacy"
    assert applied_control["market_ok_hysteresis_bars"] == 1


def test_managed_initial_floor_price_v2_vs_legacy():
    entry_price = 100.0

    # Legacy: 2.0% initial stop loss clamps the floor to 98.0
    legacy_cfg = {
        "initial_stop_loss_percent": 2.0,
        "catastrophic_stop_loss_percent": 8.0,
        "stop_mode": "legacy",
    }
    legacy_floor = managed_initial_floor_price(legacy_cfg, entry_price)
    assert legacy_floor == 98.0

    # v2: Stop Mode v2 drops the 2% clamp and uses calibrated 3.5% structural stop (96.5)
    v2_cfg = {
        "initial_stop_loss_percent": 2.0,
        "catastrophic_stop_loss_percent": 8.0,
        "calibrated_structural_stop_percent": 3.5,
        "stop_mode": "v2",
    }
    v2_floor = managed_initial_floor_price(v2_cfg, entry_price)
    assert v2_floor == 96.5

    # If plan has a planned stop (e.g. 96.50), v2 respects it
    plan = {"stop_price": 96.50}
    assert managed_initial_floor_price(v2_cfg, entry_price, plan=plan) == 96.50


def test_ensure_position_episode_records_rollout_cohort():
    config = {
        "symbol": "CANARY_SYM",
        "rollout_cohort": "canary",
        "stop_mode": "v2",
        "target_mode": "v2",
        "calibrated_structural_stop_percent": 3.5,
    }
    state = {}
    position = {"symbol": "CANARY_SYM", "qty": "50", "avg_entry_price": "200.00"}
    episode = ensure_position_episode(config, state, position)

    assert episode["rollout_cohort"] == "canary"
    assert episode["stop_mode"] == "v2"
    assert episode["target_mode"] == "v2"
