import math
from traderbot.core_strategy_engine.entry_models.entry_safety import reentry_policy, EASTERN

from traderbot.core_strategy_engine.entry_models.filters import evaluate_entry_filters


def market_ok_at(market_bars, timestamp):
    """Return the completed-bar trend regime at ``timestamp``."""
    prior = [bar for bar in market_bars if bar["t"] <= timestamp]
    if len(prior) < 6:
        return True
    bar = prior[-1]
    previous = prior[-6]
    ema21_slope = (bar["ema21"] - previous["ema21"]) / previous["ema21"]
    return bar["c"] > bar["vwap"] and bar["c"] > bar["ema21"] and ema21_slope >= 0


def allowed_ledger_price(exit_price, realized_pl=0, strong=False):
    if realized_pl >= 0:
        premium = 1.025 if strong else 1.01
    else:
        premium = 0.985
    return exit_price * premium


def build_entry_features(
    symbol,
    bars,
    market_bars,
    exit_trade=None,
    ignore_ledger=False,
    sector_bars=None,
    config=None,
    filter_context=None,
):
    """Build the model-neutral facts used to evaluate an entry.

    This function is deliberately pure: callers provide completed indicator
    bars and receive an internal feature snapshot without broker or state
    mutations. Pullback zones, breakout triggers, and model-specific risk
    geometry belong to their candidate evaluators and are not calculated here.
    """
    if len(bars) < 51:
        return None

    config = config or {}
    index = len(bars) - 1
    bar = bars[index]
    previous = bars[index - 1]
    prior_20_bars = bars[max(0, index - 20) : index]
    atr = max(float(bar["atr14"]), 0.01)
    ema21_slope = (
        (bar["ema21"] - bars[index - 5]["ema21"])
        / bars[index - 5]["ema21"]
    )
    recent_high = max(item["h"] for item in prior_20_bars)
    recent_low = min(item["l"] for item in prior_20_bars)
    strong_volume = bar["volume_ratio"] >= 1.5
    policy = reentry_policy(exit_trade, bar["t"], (filter_context or {}).get("sessions"))
    ledger_ignored = bool(ignore_ledger or not exit_trade or policy["cap_expired"])
    ledger_cap = (
        math.inf
        if ledger_ignored
        else allowed_ledger_price(
            exit_trade["exit_price"],
            exit_trade.get("realized_pl", 0),
            strong=strong_volume,
        )
    )
    market_ok = market_ok_at(market_bars, bar["t"])
    sector_ok = market_ok_at(sector_bars, bar["t"]) if sector_bars else True
    regime_ok = (
        (market_ok or not config.get("dynamic_require_market_regime", True))
        and (sector_ok or not config.get("dynamic_require_sector_regime", True))
    )
    same_day_exit = bool(
        exit_trade
        and exit_trade.get("exit_time")
        and bar["t"].astimezone(EASTERN).date() == exit_trade["exit_time"].astimezone(EASTERN).date()
    )
    above_exit = True  # Current setup geometry replaces the old exit-price floor.
    no_same_day_loss_reentry = not (
        exit_trade and exit_trade.get("realized_pl", 0) < 0 and same_day_exit
    )

    features = {
        "symbol": symbol,
        "as_of": bar["t"],
        "bar": bar,
        "previous_bar": previous,
        "prior_20_bars": prior_20_bars,
        "bars": bars,
        "market_bars": market_bars,
        "sector_bars": sector_bars or market_bars,
        "recent_three_bars": bars[index - 2 : index + 1],
        "atr14": atr,
        "ema21_slope_5bars": ema21_slope,
        "ema21_change_5bars": bar["ema21"] - bars[index - 5]["ema21"],
        "trend_atr14": bar.get("atr14"),
        "previous_atr14": previous.get("atr14"),
        "recent_high_20": recent_high,
        "recent_low_20": recent_low,
        "relative_dollar_volume": bar["volume_ratio"],
        "strong_volume": strong_volume,
        "market_ok": market_ok,
        "sector_ok": sector_ok,
        "regime_ok": regime_ok,
        "ledger_ignored": ledger_ignored,
        "reentry_policy": policy,
        "ledger_cap": ledger_cap,
        "same_day_exit": same_day_exit,
        "above_exit": above_exit,
        "no_same_day_loss_reentry": no_same_day_loss_reentry,
        "event_calendar": (filter_context or {}).get("event_calendar"),
    }
    features["entry_filters"] = evaluate_entry_filters(
        features, config=config, context=filter_context
    )
    return features


__all__ = [
    "allowed_ledger_price",
    "build_entry_features",
    "market_ok_at",
]
