"""Out-of-sample admission gates for incremental signal families."""

import math
import statistics


def return_correlation(existing_returns, candidate_returns):
    keys = sorted(set(existing_returns) | set(candidate_returns))
    if len(keys) < 2:
        return None
    left = [float(existing_returns.get(key, 0)) for key in keys]
    right = [float(candidate_returns.get(key, 0)) for key in keys]
    if len(set(left)) < 2 or len(set(right)) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    covariance = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return round(covariance / denominator, 6) if denominator else None


def daily_trade_returns(report):
    equity = float(report.get("account_equity_assumption") or 1)
    values = {}
    for trade in report.get("trades") or []:
        timestamp = trade.get("exit_time") or trade.get("signal_time") or ""
        day = str(timestamp)[:10]
        values[day] = values.get(day, 0) + float(trade.get("pnl") or 0) / equity
    return values


def evaluate_model_admission(existing, isolated, combined, minimum_trades=20, maximum_correlation=0.8):
    existing_metrics = existing.get("aggregate_baseline_metrics") or existing.get("baseline_metrics") or {}
    isolated_metrics = isolated.get("aggregate_baseline_metrics") or isolated.get("baseline_metrics") or {}
    combined_metrics = combined.get("aggregate_baseline_metrics") or combined.get("baseline_metrics") or {}
    correlation = return_correlation(
        daily_trade_returns(existing), daily_trade_returns(isolated)
    )
    reasons = []
    if int(isolated_metrics.get("trades") or 0) < int(minimum_trades):
        reasons.append("insufficient_isolated_oos_trades")
    if float(isolated_metrics.get("net_pnl_after_estimated_slippage") or 0) <= 0:
        reasons.append("isolated_oos_return_not_positive")
    if combined_metrics.get("sharpe_ratio") is None or existing_metrics.get("sharpe_ratio") is None:
        reasons.append("combined_sharpe_unavailable")
    elif float(combined_metrics["sharpe_ratio"]) <= float(existing_metrics["sharpe_ratio"]):
        reasons.append("no_incremental_risk_adjusted_return")
    if correlation is None:
        reasons.append("return_correlation_unavailable")
    elif abs(correlation) > float(maximum_correlation):
        reasons.append("return_correlation_too_high")
    return {
        "admitted": not reasons,
        "reasons": reasons,
        "return_correlation": correlation,
        "minimum_trades": int(minimum_trades),
        "maximum_absolute_correlation": float(maximum_correlation),
        "existing_metrics": existing_metrics,
        "isolated_model_metrics": isolated_metrics,
        "combined_metrics": combined_metrics,
    }


__all__ = ["evaluate_model_admission", "return_correlation"]
