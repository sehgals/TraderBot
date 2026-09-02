import datetime

from traderbot.core_strategy_engine.engine import evaluate_flat_entry_eligibility


def active_plan(as_of):
    return {"status": "active_signal", "last_bar_time": as_of.isoformat()}


def test_portfolio_authorization_is_bound_to_exact_candidate_bar():
    now = datetime.datetime.now(datetime.timezone.utc)
    plan = active_plan(now)
    config = {
        "dynamic_entry_enabled": True,
        "portfolio_allocation_required": True,
    }
    blocked = evaluate_flat_entry_eligibility(
        config, {}, plan, now=now, mode="new_entry"
    )
    wrong_bar = evaluate_flat_entry_eligibility(
        {
            **config,
            "portfolio_allocation_authorized": True,
            "portfolio_allocation_as_of": (now - datetime.timedelta(minutes=5)).isoformat(),
        },
        {}, plan, now=now, mode="new_entry",
    )
    approved = evaluate_flat_entry_eligibility(
        {
            **config,
            "portfolio_allocation_authorized": True,
            "portfolio_allocation_as_of": plan["last_bar_time"],
        },
        {}, plan, now=now, mode="new_entry",
    )

    assert "portfolio_allocation_required" in blocked["reasons"]
    assert "portfolio_allocation_required" in wrong_bar["reasons"]
    assert approved["eligible"] is True
