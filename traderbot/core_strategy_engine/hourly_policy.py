"""Completed-bar confirmation and entry trend policy shared with research."""
import datetime as dt
import math

from traderbot.core_strategy_engine.position_health import context_is_fresh, parse_time


TREND_MODELS = frozenset({"breakout_continuation", "pullback_reclaim"})


def advance_confirmation(previous, assessment, episode_id, timeframe_minutes=60):
    """Advance only on a new, adjacent, valid bar within one position episode."""
    previous = dict(previous or {})
    # Old state cannot establish a safe sequence across a version upgrade.
    if previous.get("episode_id") != episode_id or previous.get("version") != 2:
        previous = {"version": 2, "episode_id": episode_id, "count": 0}
    if not assessment.get("data_fresh") or not assessment.get("data_complete"):
        return previous
    bar_id = assessment.get("bar_id")
    try:
        stamp = parse_time(assessment.get("as_of"))
        last = parse_time(previous.get("last_bar_at"))
    except (ValueError, TypeError):
        return previous
    if not bar_id or bar_id == previous.get("last_bar_id") or not stamp or (last and stamp <= last):
        return previous
    action = assessment.get("recommended_action")
    adjacent = last and stamp - last == dt.timedelta(minutes=timeframe_minutes)
    count = 0
    if action in ("reduce", "exit"):
        count = previous.get("count", 0) + 1 if adjacent and previous.get("action") == action else 1
    return {"version": 2, "episode_id": episode_id, "action": action,
            "count": count, "last_bar_id": bar_id, "last_bar_at": stamp.isoformat()}


def hourly_gate_mode(config):
    mode = (config.get("position_health") or {}).get("entry_gate_mode", "off")
    if mode not in ("off", "shadow", "enforce"):
        raise ValueError("entry_gate_mode must be off, shadow, or enforce")
    return mode


def hourly_entry_assessment(context, now, max_age_bars=2):
    context = context or {}
    bar = context.get("latest_bar") or {}
    reasons = []
    values = [bar.get(k) for k in ("c", "ema21", "ema50", "ema21_slope")]
    complete = all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)
    complete = complete and all(v > 0 for v in values[:3])
    try:
        now = parse_time(now)
        stamp = parse_time(context.get("as_of"))
        complete_bar = stamp is not None and stamp + dt.timedelta(hours=1) <= now
        fresh = complete_bar and context_is_fresh(
            context.get("as_of"), 60, max_age_bars, now, context.get("session_close"))
    except (ValueError, TypeError):
        fresh = False
    if not complete:
        reasons.append("hourly_trend_data_unavailable")
    if not fresh:
        reasons.append("hourly_trend_data_stale")
    checks = {}
    if complete:
        checks = {"price_below_ema21": values[0] < values[1],
                  "price_below_ema50": values[0] < values[2],
                  "ema21_falling": values[3] < 0}
        if all(checks.values()):
            reasons.append("hourly_structural_trend_failure")
    return {"passed": not reasons, "reasons": reasons, "as_of": context.get("as_of"),
            "bar_id": context.get("bar_id"), "checks": checks,
            "data_complete": complete, "data_fresh": bool(fresh)}


def apply_hourly_entry_gate(candidate, assessment, mode):
    if mode == "off" or candidate.get("model_id") not in TREND_MODELS:
        return candidate
    candidate["hourly_entry_gate"] = {**assessment, "mode": mode}
    if mode == "enforce":
        candidate.setdefault("checks", {})["hourly_trend_ok"] = assessment["passed"]
        candidate.setdefault("hard_checks", {})["hourly_trend_ok"] = assessment["passed"]
        if not assessment["passed"]:
            for key in ("hard_blockers", "blockers", "decision_reasons"):
                candidate[key] = sorted(set(candidate.get(key) or []) | set(assessment["reasons"]))
            candidate["status"] = "watch"
    return candidate
