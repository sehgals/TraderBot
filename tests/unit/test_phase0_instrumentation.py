import datetime
from traderbot.core_strategy_engine import engine


class DummyClient:
    def __init__(self, position=None, open_orders=None):
        self._position = position or {}
        self._open_orders = open_orders or []

    def position(self, symbol):
        return self._position

    def order(self, order_id):
        return None

    def open_stop_orders(self, symbol):
        return []

    def open_buy_orders(self, symbol):
        return []


def test_dynamic_entry_reference_qty_prefers_target_qty_over_stale_exit_qty():
    client = DummyClient(position={"qty": "0"})
    config = {"symbol": "NVDA"}
    # Prior partial/reduced exit left last_exit_qty as 2 shares
    state = {"last_exit_qty": 2, "active_stop_qty": 0}
    # Risk-budgeted target_qty is 15 shares
    ref_qty = engine.dynamic_entry_reference_qty(client, config, state, fallback_qty=15)
    assert ref_qty == 15, "Should use target fallback_qty to prevent starvation by prior partial exits"


def test_dynamic_entry_reference_qty_falls_back_when_no_target():
    client = DummyClient(position={"qty": "0"})
    config = {"symbol": "NVDA"}
    state = {"last_exit_qty": 3}
    ref_qty = engine.dynamic_entry_reference_qty(client, config, state, fallback_qty=0)
    assert ref_qty == 3


def test_ensure_position_episode_populates_instrumentation():
    config = {"symbol": "MSFT"}
    plan = {
        "score": 85,
        "status": "active",
        "mode": "breakout",
        "last_bar_time": "2026-10-16T14:30:00Z",
        "atr14": 4.50,
        "stop_price": 385.0,
        "risk_per_share": 15.0,
    }
    state = {
        "pending_entry_requested_qty": 10,
        "dynamic_entry_plan": plan,
    }
    position = {"qty": "10", "avg_entry_price": "400.0"}
    entry_order = {"id": "ord-1", "qty": "10", "status": "filled"}

    episode = engine.ensure_position_episode(
        config=config,
        state=state,
        position=position,
        entry_order=entry_order,
    )

    assert episode["symbol"] == "MSFT"
    assert episode["requested_qty"] == 10
    assert episode["initial_qty"] == 10
    assert episode["requested_notional"] == 4000.0
    assert episode["filled_notional"] == 4000.0
    assert episode["requested_risk_dollars"] == 150.0  # 10 * 15.0
    assert episode["filled_risk_dollars"] == 150.0
    assert episode["stop_loss_percent"] == 3.75
    assert episode["atr_percent"] == 1.125  # 4.5 / 400.0 * 100
    assert episode["mfe_percent"] == 0.0
    assert episode["mae_percent"] == 0.0
    assert episode["mfe_r"] == 0.0
    assert episode["mae_r"] == 0.0


def test_reconcile_flat_position_state_calculates_holding_hours():
    client = DummyClient(position={"qty": "0"})
    state = {
        "position_episode": {
            "episode_id": "ep-123",
            "symbol": "AAPL",
            "opened_at": "2026-10-15T14:00:00+00:00",
            "exit_events": [],
        },
        "last_exit_qty": 5,
        "entry_fill_price": 200.0,
    }

    engine.reconcile_flat_position_state(client, "AAPL", state)
    closed = state.get("closed_position_episodes")
    assert closed is not None and len(closed) == 1
    # holding_hours should be computed and float > 0
    assert isinstance(closed[0].get("holding_hours"), float)
    assert closed[0]["holding_hours"] > 0
    assert state.get("position_episode") is None


def test_cap_qty_uses_custom_equity():
    from scripts.run_risk_control_backtest import cap_qty

    config = {"max_symbol_notional_percent": 10}  # 10% of equity
    # With 100k equity: 10k notional / $100 = 100 shares
    assert cap_qty(config, desired_qty=200, price=100.0, current_qty=0, equity=100000.0) == 100
    # With 50k live equity: 5k notional / $100 = 50 shares
    assert cap_qty(config, desired_qty=200, price=100.0, current_qty=0, equity=50000.0) == 50


def test_ledger_build_report_includes_episode_metrics():
    from scripts.analyze_exit_ledger import build_report

    t_entry = datetime.datetime(2026, 10, 15, 14, 0, tzinfo=datetime.timezone.utc)
    t_exit = datetime.datetime(2026, 10, 15, 16, 30, tzinfo=datetime.timezone.utc)
    trades = [{
        "symbol": "AAPL",
        "episode_id": "AAPL:ord-1",
        "exit_order_id": "ord-2",
        "entry_time": t_entry,
        "exit_time": t_exit,
        "qty": 10,
        "realized_pl": 50.0,
        "exit_reason": "profit_tranche_2r",
        "exit_reason_source": "watcher_episode",
    }]
    episodes_meta = {
        "AAPL:ord-1": {
            "holding_hours": 2.5,
            "mfe_percent": 2.1,
            "mae_percent": 0.4,
            "mfe_r": 1.8,
            "mae_r": 0.3,
        }
    }
    report = build_report(fills=[], trades=trades, rows=[], start="2026-10-15T00:00:00Z", end="2026-10-16T00:00:00Z", episodes_meta=episodes_meta)
    assert len(report["episodes"]) == 1
    ep = report["episodes"][0]
    assert ep["episode_id"] == "AAPL:ord-1"
    assert ep["holding_hours"] == 2.5
    assert ep["mfe_percent"] == 2.1
    assert ep["mae_percent"] == 0.4
    assert ep["mfe_r"] == 1.8
    assert ep["mae_r"] == 0.3

