from traderbot.backtester.regime_comparison import compare_regime_backtests


def report(sharpe, drawdown, pnl, trades=30, exposure=40):
    return {"baseline_metrics": {
        "sharpe_ratio": sharpe,
        "maximum_drawdown_percent": drawdown,
        "net_pnl_after_estimated_slippage": pnl,
        "trades": trades,
        "average_gross_exposure_percent": exposure,
    }}


def test_regime_bands_require_risk_adjusted_improvement_not_more_exposure():
    result = compare_regime_backtests(
        report(1.0, 8, 1000, exposure=30),
        report(0.9, 7, 1200, exposure=50),
    )
    assert result["accepted"] is False
    assert result["reasons"] == ["sharpe_not_improved"]


def test_regime_bands_accept_only_adequate_positive_sample():
    accepted = compare_regime_backtests(report(1, 8, 1000), report(1.2, 7, 1100))
    insufficient = compare_regime_backtests(
        report(1, 8, 1000), report(1.2, 7, 1100, trades=2)
    )
    assert accepted["accepted"] is True
    assert insufficient["accepted"] is False
    assert "insufficient_banded_trades" in insufficient["reasons"]
