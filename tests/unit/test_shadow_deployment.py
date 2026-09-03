from traderbot.shadow_deployment import (
    apply_promotion_stage,
    promote_stage,
    promotion_eligibility,
    update_shadow_ledger,
)


def decision(as_of="2026-09-03T14:30:00Z", price=99):
    candidate = {
        "symbol": "AAA", "model_id": "relative_strength_continuation",
        "candidate_as_of": as_of, "limit_price": 100, "last_price": price,
        "notional": 1000, "score": 90, "rank_value": 180,
    }
    return {
        "candidate_bar_times": [as_of], "ranked": [candidate],
        "selected": [candidate], "rejected": [{"symbol": "BBB"}],
        "incumbent_executions": [],
    }


def test_shadow_ledger_records_order_later_fill_slippage_and_alternative():
    state = {}
    first = update_shadow_ledger(state, decision(price=101), {"shadow_slippage_bps": 5})
    later = decision("2026-09-03T14:35:00Z", price=99)
    later["selected"] = []
    second = update_shadow_ledger(state, later, {"shadow_slippage_bps": 5})
    order = next(iter(second["orders"].values()))
    assert first["signals"] == 1
    assert order["status"] == "filled"
    assert order["fill_price"] > 99
    assert second["estimated_slippage"] > 0
    assert second["rejected_alternatives"] == 2


def test_shadow_promotion_requires_sessions_or_signals_and_a_fill():
    settings = {
        "promotion_stage": "shadow", "minimum_shadow_sessions": 20,
        "minimum_shadow_signals": 30,
    }
    blocked = promotion_eligibility({"shadow_ledger": {}}, settings, "small_notional")
    allowed_state = {"shadow_ledger": {
        "session_count": 20, "signals": 2, "filled_orders": 1,
    }}
    allowed = promotion_eligibility(allowed_state, settings, "small_notional")
    assert blocked["eligible"] is False
    assert allowed["eligible"] is True


def test_promotion_is_one_stage_at_a_time_and_does_not_mutate_failed_config():
    config = {"portfolio_allocator": {"promotion_stage": "shadow"}}
    unchanged, _, result = promote_stage(config, {}, "partial_allocation")
    assert result["eligible"] is False
    assert unchanged == config


def test_partial_stage_deterministically_limits_selected_allocations():
    item = decision()["selected"][0]
    source = {"selected": [
        {**item, "symbol": "AAA"}, {**item, "symbol": "BBB"},
        {**item, "symbol": "CCC"}, {**item, "symbol": "DDD"},
    ], "rejected": []}
    result = apply_promotion_stage(source, {
        "promotion_stage": "partial_allocation", "partial_allocation_fraction": 0.5,
    })
    assert [item["symbol"] for item in result["selected"]] == ["AAA", "BBB"]
    assert len(result["rejected"]) == 2
