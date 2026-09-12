from traderbot.portfolio_allocator import allocate_candidates, classify_exposure_regime
from traderbot.monitoring import supervisor
from unittest.mock import patch


def candidate(symbol, score, rr=2, notional=1000, risk=100, sector="Tech", factor="Growth"):
    return {
        "symbol": symbol, "score": score, "expected_reward_risk": rr,
        "notional": notional, "risk_dollars": risk, "sector": sector,
        "factor": factor, "hard_safety_passed": True,
    }


def portfolio(**overrides):
    result = {
        "equity": 10_000, "cash": 10_000, "position_count": 0,
        "aggregate_open_risk": 0, "daily_entries": 0, "daily_turnover": 0,
        "symbol_exposure": {}, "sector_exposure": {}, "factor_exposure": {},
    }
    result.update(overrides)
    return result


def test_allocation_is_deterministic_and_ranks_risk_adjusted_value():
    items = [candidate("BBB", 90, 1.5), candidate("AAA", 85, 2.0)]
    first = allocate_candidates(items, portfolio())
    second = allocate_candidates(list(reversed(items)), portfolio())
    assert [x["symbol"] for x in first["ranked"]] == ["AAA", "BBB"]
    assert [x["symbol"] for x in first["selected"]] == [x["symbol"] for x in second["selected"]]


def test_hard_safety_and_minimum_score_cannot_be_bypassed():
    unsafe = candidate("BAD", 100)
    unsafe["hard_safety_passed"] = False
    result = allocate_candidates([unsafe, candidate("WEAK", 79)], portfolio())
    assert result["selected"] == []
    assert {x["reason"] for x in result["rejected"]} == {
        "hard_safety_not_passed", "score_below_minimum"
    }


def test_allocator_enforces_cash_symbol_sector_factor_and_risk_limits():
    settings = {
        "maximum_symbol_notional_percent": 10,
        "maximum_sector_exposure_percent": 20,
        "maximum_correlated_factor_exposure_percent": 20,
        "maximum_aggregate_open_risk_percent": 2,
        "minimum_cash_reserve_percent": 20,
    }
    result = allocate_candidates(
        [candidate("AAA", 95), candidate("BBB", 90), candidate("CCC", 85)],
        portfolio(), settings,
    )
    assert [x["symbol"] for x in result["selected"]] == ["AAA", "BBB"]
    assert result["rejected"][0]["reason"] == "maximum_aggregate_open_risk"


def test_allocator_enforces_position_daily_turnover_and_score_separation():
    result = allocate_candidates(
        [candidate("AAA", 91), candidate("BBB", 90)],
        portfolio(position_count=1, daily_entries=0),
        {
            "maximum_simultaneous_positions": 2,
            "maximum_daily_entries": 1,
            "maximum_daily_turnover_percent": 10,
            "minimum_score_separation": 2,
        },
    )
    assert result["selected"] == []
    assert any(x["reason"] == "minimum_score_separation" for x in result["rejected"])


class BrokerSnapshot:
    def account(self):
        return {"equity": "10000", "cash": "10000"}

    def positions(self):
        return []

    def open_orders(self):
        return []


def allocation_record(as_of="2026-09-02T14:30:00+00:00"):
    return {
        "symbol": "AAA",
        "strategy_config": {
            "portfolio_allocation_required": True,
            "dynamic_entry_notional": 1000,
        },
        "result": {"dynamic_plan": {
            "setup_score": 90,
            "expected_reward_risk": 2,
            "limit_price": 100,
            "risk_per_share": 5,
            "hard_blockers": [],
            "last_bar_time": as_of,
            "model_id": "pullback_reclaim",
        }},
    }


def test_supervisor_shadow_allocation_is_once_per_completed_bar(tmp_path):
    watcher = {
        "symbol": "AAA", "group": "new", "state_path": tmp_path / "aaa.json"
    }
    config = {"portfolio_allocator": {"enabled": True, "shadow_mode": True}}
    first, executions = supervisor.run_portfolio_allocator(
        BrokerSnapshot(), [allocation_record()], config, [watcher], tmp_path, {"timestamp":"2026-09-02T14:35:00+00:00"}
    )
    second, _ = supervisor.run_portfolio_allocator(
        BrokerSnapshot(), [allocation_record()], config, [watcher], tmp_path, {"timestamp":"2026-09-02T14:35:00+00:00"}
    )
    assert [item["symbol"] for item in first["selected"]] == ["AAA"]
    assert executions == []
    assert second["status"] == "candidate_snapshot_already_allocated"


def test_supervisor_live_allocation_issues_bar_bound_authorization(tmp_path):
    watcher = {
        "symbol": "AAA", "group": "new", "state_path": tmp_path / "aaa.json"
    }
    config = {"portfolio_allocator": {"enabled": True, "shadow_mode": False}}
    submitted = {
        "symbol": "AAA",
        "strategy_config": {},
        "result": {"status": "dynamic_reentry_order_submitted"},
    }
    with patch.object(supervisor, "run_watcher", return_value=submitted) as runner:
        decision, executions = supervisor.run_portfolio_allocator(
            BrokerSnapshot(), [allocation_record()], config, [watcher], tmp_path, {"timestamp":"2026-09-02T14:35:00+00:00"}
        )

    overrides = runner.call_args.kwargs["config_overrides"]
    assert overrides["portfolio_allocation_authorized"] is True
    assert overrides["portfolio_allocation_as_of"] == "2026-09-02T14:30:00+00:00"
    assert decision["executions"] == [
        {"symbol": "AAA", "status": "dynamic_reentry_order_submitted"}
    ]
    assert len(executions) == 1


def market_bars(close, ema21, ema50, slope=1):
    return [
        {"t": index, "c": close, "ema21": ema21 + index * slope, "ema50": ema50}
        for index in range(6)
    ]


def test_regime_classification_uses_only_bars_available_at_decision_time():
    bars = market_bars(120, 100, 90)
    bars.append({"t": 6, "c": 80, "ema21": 90, "ema50": 100})
    assert classify_exposure_regime(bars, timestamp=5) == "favorable"
    assert classify_exposure_regime(bars, timestamp=6) == "defensive"


def test_regime_ceiling_blocks_without_forcing_target_floor():
    base = portfolio(gross_exposure=2400, regime="defensive")
    blocked = allocate_candidates([candidate("AAA", 95)], base)
    empty = allocate_candidates([], portfolio(gross_exposure=0, regime="favorable"))

    assert blocked["selected"] == []
    assert blocked["rejected"][0]["reason"] == "regime_exposure_ceiling"
    assert empty["selected"] == []
    assert empty["below_target_band"] is True
