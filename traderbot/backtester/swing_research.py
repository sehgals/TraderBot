"""Opt-in research variants; never loaded by the live execution path."""
import copy
import math
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from traderbot.core_strategy_engine.entry_models.candidate import weighted_factor_score
from traderbot.core_strategy_engine.hourly_policy import hourly_entry_assessment


VARIANTS = ("confirmation_only", "hourly_gate", "hourly_swing", "hourly_swing_slow_trail")


def research_configs(configs, variant):
    if variant not in VARIANTS:
        raise ValueError(f"Unknown research variant: {variant}")
    result = copy.deepcopy(configs)
    for config in result.values():
        config.setdefault("position_health", {})["entry_gate_mode"] = (
            "off" if variant == "confirmation_only" else "enforce")
        config.pop("research_swing_stops", None)
        if variant.startswith("hourly_swing"):
            config["research_swing_stops"] = {"atr_multiple": 2.5, "structure_buffer_atr": 0.1,
                "slow_trail": variant.endswith("slow_trail"), "trail_activation_r": 2.0}
    return result


def transform_swing_candidate(candidate, config, context, now):
    """Widen to hourly structure/ATR, preserve targets, and requalify risk."""
    settings = config.get("research_swing_stops")
    if not settings:
        return
    bar = (context or {}).get("latest_bar") or {}
    atr = float(bar.get("atr14") or 0)
    entry = float(candidate.get("limit_price") or 0)
    old_stop = float(candidate.get("stop_price") or 0)
    assessment = hourly_entry_assessment(context, now)
    valid = assessment["data_fresh"] and assessment["data_complete"] and math.isfinite(atr) and atr > 0
    reason = None
    if not valid or not 0 < old_stop < entry:
        reason = "swing_stop_data_unavailable"
    else:
        low = float((context or {}).get("structure_low") or bar.get("l") or 0)
        if not math.isfinite(low) or low <= 0:
            reason = "swing_stop_data_unavailable"
        else:
            catastrophic_pct = float(config.get("catastrophic_stop_loss_percent", 8))
            raw_stop = min(old_stop, entry - atr * settings["atr_multiple"],
                           low - atr * settings["structure_buffer_atr"])
            if raw_stop < entry * (1 - catastrophic_pct / 100):
                reason = "swing_stop_exceeds_catastrophic_limit"
            else:
                # Round down to a cent before sizing, so actual risk is not understated.
                stop = math.floor(raw_stop * 100) / 100
                if stop < entry * (1 - catastrophic_pct / 100):
                    reason = "swing_stop_exceeds_catastrophic_limit"
                else:
                    risk = entry - stop
                    rr = (float(candidate.get("target_price") or 0) - entry) / risk
                    minimum = max(float(candidate.get("minimum_reward_risk") or 1.5),
                                  float(candidate.get("absolute_minimum_reward_risk") or 1))
                    candidate.update(stop_price=stop, risk_per_share=risk, expected_reward_risk=rr,
                                     research_original_stop=old_stop, research_stop_as_of=context.get("as_of"))
                    scores = candidate.get("factor_scores") or {}
                    if scores:
                        scores["reward_risk_geometry"] = max(0, min(100, 100 * rr / minimum))
                        score, weights, contributions = weighted_factor_score(scores, candidate.get("factor_weights"))
                        candidate.update(setup_score=score, factor_weights=weights, factor_contributions=contributions,
                                         setup_score_meets_threshold=score >= candidate.get("minimum_setup_score", 80))
                        if not candidate["setup_score_meets_threshold"]:
                            reason = "setup_score_below_minimum"
                    for key in ("checks", "hard_checks"):
                        candidate.setdefault(key, {})["swing_reward_risk_ok"] = rr >= minimum
                    if rr < minimum:
                        reason = "swing_reward_risk_too_low"
    if reason:
        candidate["status"] = "watch"
        for key in ("blockers", "hard_blockers", "decision_reasons"):
            candidate[key] = sorted(set(candidate.get(key) or []) | {reason})


def lifecycle_metrics(trades):
    reasons = Counter(t.get("exit_reason", "unknown") for t in trades)
    hours = []
    last_exit = {}
    same_day_reentries = 0
    for trade in sorted(trades, key=lambda t: t["entry_time"]):
        entered = datetime.fromisoformat(trade["entry_time"].replace("Z", "+00:00"))
        exited = datetime.fromisoformat(trade["exit_time"].replace("Z", "+00:00"))
        hours.append((exited - entered).total_seconds() / 3600)
        prior = last_exit.get(trade["symbol"])
        if (prior and prior[0] <= entered
                and prior[0].astimezone(ZoneInfo("America/New_York")).date()
                == entered.astimezone(ZoneInfo("America/New_York")).date()
                and prior[1] == "stop_floor"):
            same_day_reentries += 1
        last_exit[trade["symbol"]] = (exited, trade.get("exit_reason"))
    return {"average_holding_hours": sum(hours) / len(hours) if hours else None,
            "exit_reasons": dict(reasons), "same_day_reentries_after_stop": same_day_reentries}
