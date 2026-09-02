"""Acceptance comparison for regime-banded versus unbanded backtests."""


def compare_regime_backtests(unbanded, banded, minimum_trades=20):
    base = unbanded.get("aggregate_baseline_metrics") or unbanded.get("baseline_metrics") or {}
    test = banded.get("aggregate_baseline_metrics") or banded.get("baseline_metrics") or {}
    reasons = []
    if int(test.get("trades") or 0) < int(minimum_trades):
        reasons.append("insufficient_banded_trades")
    if base.get("sharpe_ratio") is None or test.get("sharpe_ratio") is None:
        reasons.append("sharpe_unavailable")
    elif float(test["sharpe_ratio"]) <= float(base["sharpe_ratio"]):
        reasons.append("sharpe_not_improved")
    if float(test.get("maximum_drawdown_percent") or 0) > float(
        base.get("maximum_drawdown_percent") or 0
    ):
        reasons.append("drawdown_worsened")
    if float(test.get("net_pnl_after_estimated_slippage") or 0) <= 0:
        reasons.append("nonpositive_pnl_after_slippage")
    return {
        "accepted": not reasons,
        "reasons": reasons,
        "minimum_trades": int(minimum_trades),
        "unbanded": base,
        "banded": test,
        "deltas": {
            "sharpe_ratio": (
                round(float(test["sharpe_ratio"]) - float(base["sharpe_ratio"]), 6)
                if base.get("sharpe_ratio") is not None and test.get("sharpe_ratio") is not None
                else None
            ),
            "maximum_drawdown_percent": round(
                float(test.get("maximum_drawdown_percent") or 0)
                - float(base.get("maximum_drawdown_percent") or 0), 6
            ),
            "average_gross_exposure_percent": round(
                float(test.get("average_gross_exposure_percent") or 0)
                - float(base.get("average_gross_exposure_percent") or 0), 6
            ),
        },
    }


__all__ = ["compare_regime_backtests"]
