"""Shadow-order ledger and staged promotion gates for portfolio allocation."""

import datetime
import math


PROMOTION_STAGES = ("shadow", "small_notional", "partial_allocation", "full")


def apply_promotion_stage(decision, settings):
    stage = settings.get("promotion_stage", "shadow")
    selected = list(decision.get("selected") or [])
    if stage == "partial_allocation" and selected:
        fraction = max(0.0, min(1.0, float(settings.get("partial_allocation_fraction", 0.5))))
        keep = max(1, math.floor(len(selected) * fraction))
        deferred = selected[keep:]
        decision["selected"] = selected[:keep]
        decision.setdefault("rejected", []).extend(
            {**item, "reason": "promotion_stage_partial_allocation"}
            for item in deferred
        )
    return decision


def update_shadow_ledger(state, decision, settings):
    ledger = dict(state.get("shadow_ledger") or {})
    ledger.setdefault("sessions", [])
    ledger.setdefault("orders", {})
    ledger.setdefault("fills", [])
    ledger.setdefault("signals", 0)
    ledger.setdefault("rejected_alternatives", 0)
    ledger.setdefault("incumbent_entries", 0)
    slippage_bps = float(settings.get("shadow_slippage_bps", 5.0))
    bar_times = decision.get("candidate_bar_times") or []
    as_of = bar_times[0] if len(bar_times) == 1 else None
    session = str(as_of)[:10] if as_of else None
    if session and session not in ledger["sessions"]:
        ledger["sessions"].append(session)

    prices = {
        item["symbol"]: float(item.get("last_price") or 0)
        for item in [
            *(decision.get("ranked") or []), *(decision.get("rejected") or [])
        ]
        if item.get("symbol")
    }
    for order_id, order in list(ledger["orders"].items()):
        if order.get("status") != "open" or not as_of or as_of <= order["placed_at"]:
            continue
        price = prices.get(order["symbol"])
        if str(as_of)[:10] != str(order["placed_at"])[:10]:
            order["status"] = "expired"
            order["expired_at"] = as_of
        elif price and price <= float(order["limit_price"]):
            fill_price = min(
                float(order["limit_price"]), price * (1 + slippage_bps / 10000)
            )
            order["status"] = "filled"
            order["filled_at"] = as_of
            order["fill_price"] = round(fill_price, 6)
            order["estimated_slippage"] = round(
                max(0.0, fill_price - price) * float(order["qty"]), 6
            )
            ledger["fills"].append(dict(order))
    for order in ledger["orders"].values():
        if order.get("status") == "filled":
            last = prices.get(order["symbol"])
            if last:
                order["last_mark_price"] = last
                order["unrealized_pnl"] = round(
                    (last - float(order["fill_price"])) * float(order["qty"]), 6
                )

    for item in decision.get("selected") or []:
        if not as_of or not item.get("limit_price"):
            continue
        order_id = f'{as_of}:{item["symbol"]}:{item.get("model_id") or "unknown"}'
        if order_id in ledger["orders"]:
            continue
        qty = math.floor(float(item.get("notional") or 0) / float(item["limit_price"]))
        if qty <= 0:
            continue
        ledger["orders"][order_id] = {
            "order_id": order_id, "symbol": item["symbol"],
            "model_id": item.get("model_id"), "placed_at": as_of,
            "limit_price": float(item["limit_price"]), "qty": qty,
            "notional": round(qty * float(item["limit_price"]), 2),
            "status": "open", "score": item.get("score"),
            "rank_value": item.get("rank_value"),
        }
        ledger["signals"] += 1
    ledger["rejected_alternatives"] += len(decision.get("rejected") or [])
    ledger["incumbent_entries"] += len(decision.get("incumbent_executions") or [])
    ledger["session_count"] = len(ledger["sessions"])
    ledger["open_orders"] = sum(
        order.get("status") == "open" for order in ledger["orders"].values()
    )
    ledger["filled_orders"] = sum(
        order.get("status") == "filled" for order in ledger["orders"].values()
    )
    ledger["hypothetical_unrealized_pnl"] = round(sum(
        float(order.get("unrealized_pnl") or 0) for order in ledger["orders"].values()
    ), 6)
    ledger["estimated_slippage"] = round(sum(
        float(order.get("estimated_slippage") or 0) for order in ledger["orders"].values()
    ), 6)
    state["shadow_ledger"] = ledger
    return ledger


def promotion_eligibility(state, settings, target_stage):
    current = settings.get("promotion_stage", "shadow")
    if target_stage not in PROMOTION_STAGES:
        return {"eligible": False, "reasons": ["unknown_target_stage"]}
    expected_index = PROMOTION_STAGES.index(current) + 1
    if expected_index >= len(PROMOTION_STAGES) or PROMOTION_STAGES[expected_index] != target_stage:
        return {"eligible": False, "reasons": ["promotion_must_advance_one_stage"]}
    ledger = state.get("shadow_ledger") or {}
    reasons = []
    if current == "shadow":
        sessions = int(ledger.get("session_count") or 0)
        signals = int(ledger.get("signals") or 0)
        if sessions < int(settings.get("minimum_shadow_sessions", 20)) and signals < int(
            settings.get("minimum_shadow_signals", 30)
        ):
            reasons.append("insufficient_shadow_sample")
        if int(ledger.get("filled_orders") or 0) == 0:
            reasons.append("no_hypothetical_fills")
    else:
        stage_entries = int((state.get("stage_live_entries") or {}).get(current, 0))
        required = int(settings.get("minimum_stage_live_entries", 5))
        if stage_entries < required:
            reasons.append("insufficient_live_stage_entries")
    if int(state.get("allocation_safety_violations") or 0) > 0:
        reasons.append("allocation_safety_violations")
    return {"eligible": not reasons, "reasons": reasons, "current_stage": current,
            "target_stage": target_stage}


def promote_stage(supervisor_config, state, target_stage):
    config = dict(supervisor_config)
    settings = dict(config.get("portfolio_allocator") or {})
    decision = promotion_eligibility(state, settings, target_stage)
    if not decision["eligible"]:
        return config, dict(state), decision
    settings["promotion_stage"] = target_stage
    config["portfolio_allocator"] = settings
    updated_state = dict(state)
    history = list(updated_state.get("promotion_history") or [])
    history.append({
        "from": decision["current_stage"], "to": target_stage,
        "promoted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    updated_state["promotion_history"] = history
    return config, updated_state, decision


__all__ = [
    "PROMOTION_STAGES", "apply_promotion_stage", "promotion_eligibility", "promote_stage",
    "update_shadow_ledger",
]
