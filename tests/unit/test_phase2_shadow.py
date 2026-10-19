"""Unit tests for Phase 2: Shadow Execution Mode & Regime Hysteresis."""
import datetime
import math
import pytest

from traderbot.core_strategy_engine.entry_models.features import market_ok_at
from traderbot.core_strategy_engine.engine import (
    dynamic_entry_plan,
    ensure_position_episode,
    run_once,
)


def _make_market_bars(count=10, ok_pattern=None):
    """Generate synthetic 5-min bars with VWAP and EMA21."""
    bars = []
    base_time = datetime.datetime(2026, 10, 19, 14, 0, tzinfo=datetime.timezone.utc)
    for i in range(count):
        t = base_time + datetime.timedelta(minutes=5 * i)
        is_ok = True
        if ok_pattern is not None and i >= count - len(ok_pattern):
            pattern_idx = i - (count - len(ok_pattern))
            is_ok = ok_pattern[pattern_idx]

        if is_ok:
            close = 105.0
            vwap = 100.0
            ema21 = 100.0
        else:
            close = 95.0
            vwap = 100.0
            ema21 = 100.0

        bars.append({
            "t": t,
            "c": close,
            "h": close + 1.0,
            "l": close - 1.0,
            "o": close,
            "vwap": vwap,
            "ema21": ema21,
            "volume_ratio": 1.5,
        })
    return bars


def test_market_ok_at_hysteresis_single_vs_multibar():
    bars = _make_market_bars(12, ok_pattern=[False, True])
    eval_time = bars[-1]["t"]

    assert market_ok_at(bars, eval_time, hysteresis_bars=1) is True
    assert market_ok_at(bars, eval_time, hysteresis_bars=2) is False

    bars_confirmed = _make_market_bars(12, ok_pattern=[True, True])
    assert market_ok_at(bars_confirmed, eval_time, hysteresis_bars=2) is True


def test_dynamic_entry_plan_shadow_execution_generation():
    config = {
        "symbol": "TEST",
        "calibrated_structural_stop_percent": 3.5,
        "structural_stop_atr_multiple": 1.5,
        "structural_stop_min_percent": 1.5,
        "structural_stop_max_percent": 2.5,
        "entry_filters": {},
    }
    bars = []
    base_time = datetime.datetime(2026, 10, 19, 14, 0, tzinfo=datetime.timezone.utc)
    for i in range(60):
        t = base_time + datetime.timedelta(minutes=5 * i)
        price = 100.0 + i * 0.1
        bars.append({
            "t": t,
            "o": price,
            "h": price + 0.5,
            "l": price - 0.5,
            "c": price,
            "ema9": price,
            "ema21": price - 0.2,
            "ema50": price - 0.5,
            "vwap": price - 0.1,
            "volume_ratio": 1.5,
            "atr14": 1.0,
        })
    market_bars = list(bars)

    plan = dynamic_entry_plan("TEST", bars, market_bars, config=config)
    assert plan["symbol"] == "TEST"

    shadow = plan.get("shadow_execution")
    assert shadow is not None
    assert shadow["calibrated_stop_percent"] == 3.5
    if plan.get("limit_price"):
        limit = plan["limit_price"]
        expected_stop = round(limit * 0.965, 2)
        expected_risk = round(limit - expected_stop, 2)
        assert shadow["shadow_stop_price"] == expected_stop
        assert shadow["shadow_risk_per_share"] == expected_risk
        assert shadow["shadow_targets"]["tranche_1_1R"] == round(limit + expected_risk, 2)
        assert shadow["shadow_targets"]["tranche_2_2R"] == round(limit + expected_risk * 2.0, 2)


def test_ensure_position_episode_shadow_tracking():
    config = {
        "symbol": "TEST",
        "calibrated_structural_stop_percent": 3.5,
    }
    state = {}
    position = {"symbol": "TEST", "qty": "100", "avg_entry_price": "100.00"}
    episode = ensure_position_episode(config, state, position)

    assert "shadow_tracking" in episode
    shadow = episode["shadow_tracking"]
    assert shadow["calibrated_stop_percent"] == 3.5
    assert shadow["shadow_stop_price"] == 96.50
    assert shadow["shadow_risk_per_share"] == 3.50
    assert shadow["shadow_targets"]["tranche_1_price"] == 103.50
    assert shadow["shadow_targets"]["tranche_2_price"] == 107.00
    assert shadow["shadow_qty"] == 100
    assert shadow["tranche_1_filled"] is False
    assert shadow["tranche_2_filled"] is False
    assert shadow["shadow_closed"] is False


class MockAlpacaClient:
    def __init__(self, price=100.0):
        self._price = price
        self.orders = []

    def latest_trade_price(self, symbol):
        return self._price

    def position(self, symbol):
        return {"symbol": symbol, "qty": "100", "avg_entry_price": "100.00"}

    def account(self):
        return {"equity": "100000.00"}

    def clock(self):
        return {"is_open": True, "timestamp": "2026-10-19T15:00:00Z"}

    def order(self, order_id):
        return {"id": order_id, "status": "filled", "filled_avg_price": "100.00", "qty": "100"}

    def open_orders(self, symbol=None):
        return []

    def open_buy_orders(self, symbol=None):
        return []

    def open_stop_loss_orders(self, symbol=None):
        return []

    def open_stop_orders(self, symbol=None):
        return []

    def submit_order(self, order_dict):
        self.orders.append(order_dict)
        return {"id": "mock_stop_123", "status": "new", **order_dict}

    def replace_order(self, order_id, order_dict):
        return {"id": order_id, "status": "replaced", **order_dict}


def test_shadow_tranche_1_execution_and_breakeven_move():
    config = {
        "symbol": "TEST",
        "entry_order_id": "test_order_1",
        "initial_stop_loss_percent": 2.0,
        "calibrated_structural_stop_percent": 3.5,
        "structural_stop_atr_multiple": 2.0,
        "structural_stop_min_percent": 2.0,
        "structural_stop_max_percent": 4.0,
        "trail_loss_percent": 2.0,
        "trail_stop_below_current_percent": 2.5,
        "trail_trigger_step_percent": 5,
        "position_health": {"enabled": False, "shadow_mode": True},
    }
    state = {
        "managed_entry_order_id": "test_order_1",
        "entry_fill_price": 100.0,
        "highest_trail_rung": 0,
        "position_episode": {
            "symbol": "TEST",
            "state": "OPEN",
            "average_entry_price": 100.0,
            "current_qty": 100,
            "initial_stop_price": 98.0,
            "initial_risk_per_share": 2.0,
            "max_price": 100.0,
            "min_price": 100.0,
            "shadow_tracking": {
                "calibrated_stop_percent": 3.5,
                "shadow_stop_price": 96.50,
                "shadow_risk_per_share": 3.50,
                "shadow_targets": {
                    "tranche_1_price": 103.50,
                    "tranche_2_price": 107.00,
                },
                "shadow_qty": 100,
                "shadow_remaining_qty": 100,
                "shadow_realized_pnl": 0.0,
                "tranche_1_filled": False,
                "tranche_2_filled": False,
                "shadow_stop_hit": False,
                "shadow_closed": False,
            },
        },
    }

    # Simulate price advancing past Tranche 1 (103.50) to 104.00
    client = MockAlpacaClient(price=104.0)
    result = run_once(client, config, state, clock={"is_open": True})

    episode = state["position_episode"]
    shadow = episode["shadow_tracking"]

    # Verify Tranche 1 filled in shadow tracking
    assert shadow["tranche_1_filled"] is True
    assert shadow["tranche_1_fill_price"] == 103.50
    # 50 shares * $3.50 gain = $175.00 realized shadow PnL
    assert shadow["shadow_realized_pnl"] == 175.00
    assert shadow["shadow_remaining_qty"] == 50
    # Shadow stop moved to break-even (100.0)
    assert shadow["shadow_stop_price"] == 100.0
    assert shadow["shadow_closed"] is False

    # Simulate price advancing past Tranche 2 (107.00) to 108.00
    client._price = 108.0
    run_once(client, config, state, clock={"is_open": True})

    # Verify Tranche 2 filled in shadow tracking
    assert shadow["tranche_2_filled"] is True
    assert shadow["tranche_2_fill_price"] == 107.00
    # 25 shares * $7.00 gain = $175.00 additional gain -> total $350.00
    assert shadow["shadow_realized_pnl"] == 350.00
    assert shadow["shadow_remaining_qty"] == 25


def test_stage_3_1_position_health_shadow_mode_global():
    import json
    from pathlib import Path
    from traderbot.core_strategy_engine.engine import position_action_intents, submit_confirmed_health_action

    # 1. Verify global config has shadow_mode: True
    watchers_cfg = json.loads(Path("config/watchers.json").read_text(encoding="utf-8"))
    assert watchers_cfg.get("position_health", {}).get("shadow_mode") is True

    # 2. Verify engine ignores confirmed exit/reduce action when shadow_mode is True
    config = {
        "symbol": "TEST",
        "position_health": {
            "enabled": True,
            "shadow_mode": True,
            "exit_confirmation_bars": 1,
            "health_reduction_fraction": 0.5,
        },
    }
    state = {
        "position_health_confirmation": {"action": "exit", "count": 2},
        "position_episode": {"symbol": "TEST", "episode_id": "ep1"},
    }
    position = {"symbol": "TEST", "qty": 100, "avg_entry_price": 50.0, "current_price": 50.0}
    health = {"recommended_action": "exit", "score": 10, "state": "Distressed"}

    # submit_confirmed_health_action should return None in shadow mode
    client = MockAlpacaClient(price=50.0)
    res = submit_confirmed_health_action(client, config, state, position, health)
    assert res is None
    assert len(client.orders) == 0

    # position_action_intents should not append health intents
    intents = position_action_intents(config, state, position, current_price=50.0, health=health)
    health_intents = [i for i in intents if i.get("source") == "position_health"]
    assert len(health_intents) == 0


