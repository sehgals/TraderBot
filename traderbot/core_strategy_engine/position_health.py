import datetime
import math
from zoneinfo import ZoneInfo

from traderbot.core_strategy_engine.assessments import PositionHealthAssessment


DEFAULT_HEALTH_CONFIG = {
    "max_data_age_bars": 2,
    "healthy_score": 80,
    "stable_score": 65,
    "watch_score": 45,
    "at_risk_score": 25,
    "hard_reduction_loss_percent": 6,
    "catastrophic_stop_loss_percent": 8,
}


EASTERN = ZoneInfo("America/New_York")


def interpolate(value, low_value, high_value, low_score, high_score):
    if high_value == low_value:
        return high_score
    fraction = (value - low_value) / (high_value - low_value)
    return low_score + fraction * (high_score - low_score)


def downside_score(entry_return_percent):
    value = float(entry_return_percent)
    if value >= 0:
        return 100
    if value >= -3:
        return round(interpolate(value, -3, 0, 70, 100))
    if value >= -6:
        return round(interpolate(value, -6, -3, 30, 70))
    if value > -8:
        return round(interpolate(value, -8, -6, 0, 30))
    return 0


def trend_health(latest_bar, relative_strength_5d, market_ok):
    checks = {
        "price_above_ema21": (latest_bar["c"] > latest_bar["ema21"], 25),
        "ema9_above_ema21": (latest_bar["ema9"] > latest_bar["ema21"], 20),
        "ema21_slope_positive": (latest_bar.get("ema21_slope", 0) > 0, 20),
        "price_above_ema50": (latest_bar["c"] > latest_bar["ema50"], 15),
        "relative_strength_positive": (relative_strength_5d > 0, 10),
        "market_regime_favorable": (bool(market_ok), 10),
    }
    score = sum(weight for passed, weight in checks.values() if passed)
    return score, {name: passed for name, (passed, _) in checks.items()}


def reward_risk_score(current_price, target_price, stop_price, entry_price):
    current_price = float(current_price)
    target_price = float(target_price)
    stop_price = float(stop_price)
    entry_price = float(entry_price)

    if target_price <= current_price:
        if stop_price >= entry_price:
            return 100, math.inf
        return 0, 0.0

    minimum_increment = max(0.01, current_price * 0.0001)
    remaining_risk = max(current_price - stop_price, minimum_increment)
    remaining_reward = target_price - current_price
    remaining_r = remaining_reward / remaining_risk
    return round(max(0, min(100, 50 * remaining_r))), remaining_r


def parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def context_is_fresh(as_of, timeframe_minutes, max_age_bars, now=None, session_close=None):
    timestamp = parse_time(as_of)
    if not timestamp:
        return False
    checked_at = now or datetime.datetime.now(datetime.timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=datetime.timezone.utc)
    checked_at = checked_at.astimezone(datetime.timezone.utc)
    if timestamp > checked_at:
        return False
    if session_close:
        close = parse_time(session_close)
        # During a market closure, require the final interval of the latest
        # completed session, rather than counting overnight/weekend hours.
        return close - datetime.timedelta(minutes=timeframe_minutes) <= timestamp < close
    allowance = datetime.timedelta(minutes=timeframe_minutes * max_age_bars + 5)
    if checked_at - timestamp <= allowance:
        return True

    # The final regular-session hourly bar is normally timestamped 15:00 ET.
    # It cannot be superseded after the 16:00 ET close, so keep it reportable
    # for the remainder of that trading date instead of aging it out.
    local_timestamp = timestamp.astimezone(EASTERN)
    local_checked_at = checked_at.astimezone(EASTERN)
    return bool(
        local_checked_at.date() == local_timestamp.date()
        and local_checked_at.time() >= datetime.time(16, 0)
        and local_timestamp.time() >= datetime.time(15, 0)
        and local_timestamp.time() < datetime.time(16, 0)
    )


def health_label(score, config):
    if score >= int(config["healthy_score"]):
        return "Healthy"
    if score >= int(config["stable_score"]):
        return "Stable"
    if score >= int(config["watch_score"]):
        return "Watch"
    if score >= int(config["at_risk_score"]):
        return "At Risk"
    return "Critical"


def evaluate_position_health(
    position,
    episode,
    context,
    protection,
    config=None,
    now=None,
):
    settings = {**DEFAULT_HEALTH_CONFIG, **(config or {})}
    symbol = position.get("symbol") or episode.get("symbol")
    qty = int(float(position.get("qty") or 0))
    entry_price = float(position.get("avg_entry_price") or episode.get("average_entry_price") or 0)
    current_price = float(position.get("current_price") or context.get("current_price") or 0)
    stop_price = float(protection.get("stop_price") or 0)
    stop_qty = int(float(protection.get("stop_qty") or 0))
    latest_bar = context.get("latest_bar") or {}
    target_price = float(episode.get("original_target_price") or 0)
    as_of = context.get("as_of")
    timeframe_minutes = int(context.get("timeframe_minutes") or 60)
    data_fresh = context_is_fresh(
        as_of,
        timeframe_minutes,
        int(settings["max_data_age_bars"]),
        now=now,
        session_close=context.get("session_close"),
    )
    required_bar_fields = ("c", "ema9", "ema21", "ema50", "ema21_slope")
    data_complete = bool(
        qty > 0
        and entry_price > 0
        and current_price > 0
        and target_price > 0
        and all(latest_bar.get(field) is not None for field in required_bar_fields)
        and context.get("relative_strength_5d") is not None
        and context.get("market_ok") is not None
    )
    reasons = []

    if not data_complete or not data_fresh:
        if not data_complete:
            reasons.append("health_data_incomplete")
        if not data_fresh:
            reasons.append("health_data_stale")
        return PositionHealthAssessment(
            symbol=symbol,
            as_of=as_of,
            bar_id=context.get("bar_id"),
            score=None,
            state="Unavailable",
            recommended_action="freeze",
            stop_price=stop_price or None,
            stop_qty=stop_qty,
            position_qty=qty,
            data_complete=data_complete,
            data_fresh=data_fresh,
            reasons=reasons,
        ).to_dict()

    entry_return_percent = (current_price / entry_price - 1) * 100
    downside = downside_score(entry_return_percent)
    trend, trend_checks = trend_health(
        latest_bar,
        float(context["relative_strength_5d"]),
        bool(context["market_ok"]),
    )
    rr, remaining_r = reward_risk_score(
        current_price,
        target_price,
        stop_price,
        entry_price,
    )
    score = round(0.45 * downside + 0.35 * trend + 0.20 * rr)

    structural_failure = (
        latest_bar["c"] < latest_bar["ema21"]
        and latest_bar["c"] < latest_bar["ema50"]
        and latest_bar["ema21_slope"] < 0
    )
    if structural_failure:
        score = min(score, int(settings["watch_score"]) - 1)
        reasons.append("structural_trend_failure")

    fully_protected = stop_price > 0 and stop_qty >= qty
    state = health_label(score, settings)
    action = "hold"
    if not fully_protected:
        state = "Unprotected"
        action = "restore_protection"
        reasons.append("stop_coverage_incomplete")
    elif entry_return_percent <= -float(settings["catastrophic_stop_loss_percent"]) + 1e-9:
        state = "Critical"
        action = "exit"
        reasons.append("catastrophic_loss_threshold")
    elif entry_return_percent <= -float(settings["hard_reduction_loss_percent"]) + 1e-9:
        state = "At Risk"
        action = "reduce"
        reasons.append("hard_reduction_threshold")
    elif state == "Critical":
        action = "exit"
    elif state == "At Risk":
        action = "reduce"
    elif state == "Watch":
        action = "watch"

    failed_trend_checks = [name for name, passed in trend_checks.items() if not passed]
    reasons.extend(failed_trend_checks)
    return PositionHealthAssessment(
        symbol=symbol,
        as_of=as_of,
        bar_id=context.get("bar_id"),
        score=score,
        state=state,
        recommended_action=action,
        downside_score=downside,
        trend_score=trend,
        reward_risk_score=rr,
        entry_return_percent=round(entry_return_percent, 4),
        remaining_r=None if math.isinf(remaining_r) else round(remaining_r, 4),
        stop_price=stop_price,
        stop_qty=stop_qty,
        position_qty=qty,
        data_complete=True,
        data_fresh=True,
        reasons=sorted(set(reasons)),
        components={
            "trend_checks": trend_checks,
            "benchmark_symbol": context.get("benchmark_symbol"),
            "target_price": target_price,
            "current_price": current_price,
            "entry_price": entry_price,
        },
    ).to_dict()


def evaluate_add_eligibility(
    health,
    entry_assessment,
    episode,
    position,
    protection,
    account_equity,
    config=None,
):
    settings = {
        "additions_enabled": False,
        "minimum_add_health_score": 80,
        "minimum_add_setup_score": 85,
        "minimum_add_remaining_r": 1.5,
        "max_adds_per_episode": 1,
        "max_add_fraction_of_initial_qty": 0.5,
        "max_symbol_risk_percent": 0.75,
        "max_symbol_notional_percent": 20,
        **(config or {}),
    }
    reasons = []
    current_price = float(position.get("current_price") or 0)
    entry_price = float(position.get("avg_entry_price") or 0)
    position_qty = int(float(position.get("qty") or 0))
    market_value = float(position.get("market_value") or current_price * position_qty)
    stop_price = float(protection.get("stop_price") or 0)
    stop_qty = int(float(protection.get("stop_qty") or 0))
    health_score = health.get("score")
    setup_score = entry_assessment.get("score")

    if not settings["additions_enabled"]:
        reasons.append("additions_disabled")
    if health_score is None or health_score < int(settings["minimum_add_health_score"]):
        reasons.append("health_score_below_add_threshold")
    if setup_score is None or setup_score < int(settings["minimum_add_setup_score"]):
        reasons.append("setup_score_below_add_threshold")
    if entry_assessment.get("status") not in ("qualified", "active_signal"):
        reasons.append("entry_setup_not_active")
    if current_price < entry_price:
        reasons.append("averaging_down_blocked")
    if health.get("remaining_r") is None or float(health["remaining_r"]) < float(
        settings["minimum_add_remaining_r"]
    ):
        reasons.append("remaining_reward_risk_too_low")
    trend_checks = (health.get("components") or {}).get("trend_checks") or {}
    for required in (
        "price_above_ema21",
        "price_above_ema50",
        "ema21_slope_positive",
        "market_regime_favorable",
    ):
        if not trend_checks.get(required, False):
            reasons.append(required)
    if episode.get("adverse_reduction_completed"):
        reasons.append("adverse_reduction_lockout")
    if episode.get("pending_action_id"):
        reasons.append("position_action_pending")
    if stop_price <= 0 or stop_qty < position_qty:
        reasons.append("stop_coverage_incomplete")
    if int(episode.get("add_count", 0)) >= int(settings["max_adds_per_episode"]):
        reasons.append("episode_add_limit_reached")

    initial_qty = max(1, int(episode.get("initial_qty") or position_qty))
    fraction_cap = max(
        0,
        math.floor(initial_qty * float(settings["max_add_fraction_of_initial_qty"])),
    )
    equity = float(account_equity or 0)
    max_symbol_risk = equity * float(settings["max_symbol_risk_percent"]) / 100
    current_risk = max(0, entry_price - stop_price) * position_qty
    available_risk = max(0, max_symbol_risk - current_risk)
    add_risk_per_share = max(0.01, current_price - stop_price)
    risk_cap = math.floor(available_risk / add_risk_per_share)
    max_notional = equity * float(settings["max_symbol_notional_percent"]) / 100
    notional_cap = math.floor(max(0, max_notional - market_value) / current_price) if current_price else 0
    eligible_qty = max(0, min(fraction_cap, risk_cap, notional_cap))
    if eligible_qty <= 0:
        reasons.append("add_risk_or_notional_budget_exhausted")

    return {
        "eligible": not reasons,
        "qty": eligible_qty if not reasons else 0,
        "reasons": sorted(set(reasons)),
        "fraction_cap_qty": fraction_cap,
        "risk_cap_qty": risk_cap,
        "notional_cap_qty": notional_cap,
        "available_symbol_risk": available_risk,
        "max_symbol_risk": max_symbol_risk,
    }


__all__ = [
    "DEFAULT_HEALTH_CONFIG",
    "context_is_fresh",
    "downside_score",
    "evaluate_add_eligibility",
    "evaluate_position_health",
    "health_label",
    "reward_risk_score",
    "trend_health",
]


def assessment_bar_status(as_of, now, session_close=None, grace_minutes=5):
    """Dashboard freshness for start-stamped, clock-aligned hourly bars."""
    bar = parse_time(as_of)
    checked = parse_time(now)
    if not bar:
        return "Assessment missing", None
    end = bar + datetime.timedelta(hours=1)
    if session_close:
        close = parse_time(session_close)
        if close - datetime.timedelta(hours=1) <= bar < close:
            return "Latest completed bar", min(end, close).isoformat()
        return "Assessment overdue", end.isoformat()
    expected = checked.replace(minute=0, second=0, microsecond=0) - datetime.timedelta(hours=1)
    if bar > expected:
        return "Assessment unavailable", end.isoformat()
    if bar == expected:
        return "Latest completed bar", end.isoformat()
    if bar == expected - datetime.timedelta(hours=1) and checked.minute < grace_minutes:
        return "Waiting for newer bar", end.isoformat()
    return "Assessment overdue", end.isoformat()
