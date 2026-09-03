import argparse
import bisect
import copy
import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_risk_control_backtest import (
    ACCOUNT_EQUITY,
    fetch_bars,
    iso_utc,
    load_env,
    position_health_config,
    simulate_portfolio,
)
from traderbot.backtester.baseline import (
    attribution_by_dimensions,
    combine_forward_return_reports,
    dataset_manifest,
    performance_metrics,
    source_provenance,
    stable_hash,
)
from traderbot.backtester.reentry_backtest import (
    isolate_entry_model_configs,
    load_strategy_configs,
)


UTC = datetime.timezone.utc

WEIGHT_PROFILES = {
    "balanced": {
        "price_action": 25, "stock_trend": 20, "market_sector_regime": 15,
        "volume_liquidity_quality": 15, "reward_risk_geometry": 15,
        "relative_strength_execution": 10,
    },
    "price_action": {
        "price_action": 35, "stock_trend": 20, "market_sector_regime": 10,
        "volume_liquidity_quality": 15, "reward_risk_geometry": 10,
        "relative_strength_execution": 10,
    },
    "trend_regime": {
        "price_action": 20, "stock_trend": 30, "market_sector_regime": 20,
        "volume_liquidity_quality": 10, "reward_risk_geometry": 10,
        "relative_strength_execution": 10,
    },
    "reward_risk": {
        "price_action": 20, "stock_trend": 15, "market_sector_regime": 10,
        "volume_liquidity_quality": 15, "reward_risk_geometry": 30,
        "relative_strength_execution": 10,
    },
}


def configs_with_score_weights(configs, weights):
    prepared = copy.deepcopy(configs)
    for config in prepared.values():
        config["entry_score_weights"] = dict(weights)
    return prepared


def calibration_objective(simulation):
    """Training-only objective: return, penalized for drawdown and no evidence."""
    portfolio = simulation["portfolio"]
    trades = len(simulation.get("trades_detail") or [])
    if not trades:
        return -1_000_000.0
    return round(
        float(portfolio.get("return_percent") or 0)
        - float(portfolio.get("max_drawdown_percent") or 0),
        6,
    )


def select_weight_profile(results):
    return max(
        results,
        key=lambda item: (
            item["objective"], item["trades"], item["profile"] == "balanced"
        ),
    )


def parse_time(value):
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def walk_forward_windows(start, end, train_days=90, test_days=30, purge_days=1):
    windows = []
    train_start = start
    while True:
        train_end = train_start + datetime.timedelta(days=train_days)
        test_start = train_end + datetime.timedelta(days=purge_days)
        test_end = min(test_start + datetime.timedelta(days=test_days), end)
        if test_start >= end or test_end <= test_start:
            break
        windows.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            }
        )
        train_start += datetime.timedelta(days=test_days)
    return windows


def prepare_filter_configs(configs, has_quotes=False, has_calendar=False):
    prepared = copy.deepcopy(configs)
    coverage = {}
    for config in prepared.values():
        settings = config.setdefault("entry_filters", {})
        for name in ("liquidity", "gap"):
            settings.setdefault(name, {})["enabled"] = True
            coverage[name] = "tested_from_5Min_bars"
        settings.setdefault("spread", {})["enabled"] = bool(has_quotes)
        coverage["spread"] = (
            "tested_from_point_in_time_quotes"
            if has_quotes
            else "not_tested_no_historical_quote_dataset"
        )
        for name in ("earnings", "corporate_actions"):
            settings.setdefault(name, {})["enabled"] = bool(has_calendar)
            coverage[name] = (
                "tested_from_point_in_time_event_calendar"
                if has_calendar
                else "not_tested_no_point_in_time_event_calendar"
            )
    return prepared, coverage


class HistoricalFilterContext:
    def __init__(self, quote_payload=None, event_calendar=None):
        self.event_calendar = event_calendar
        self.quotes = defaultdict(list)
        for quote in (quote_payload or {}).get("quotes", []):
            item = dict(quote)
            item["_time"] = parse_time(item["timestamp"])
            self.quotes[str(item["symbol"]).upper()].append(item)
        for items in self.quotes.values():
            items.sort(key=lambda item: item["_time"])

    def at(self, symbol, timestamp):
        context = {"evaluated_at": timestamp}
        if self.event_calendar is not None:
            context["event_calendar"] = self.event_calendar
        items = self.quotes.get(symbol.upper(), [])
        if items:
            times = [item["_time"] for item in items]
            index = bisect.bisect_right(times, timestamp) - 1
            if index >= 0:
                context["quote"] = items[index]
        return context


def slice_bars(bars, start, end):
    return [bar for bar in bars if start <= bar["t"] < end]


def regime_at(market_bars, timestamp):
    times = [bar["t"] for bar in market_bars]
    index = bisect.bisect_right(times, timestamp) - 1
    if index < 5:
        return "unknown"
    bar = market_bars[index]
    previous = market_bars[index - 5]
    slope = (bar["ema21"] - previous["ema21"]) / previous["ema21"]
    if bar["c"] > bar["ema21"] > bar["ema50"] and slope >= 0:
        return "bull"
    if bar["c"] < bar["ema21"] < bar["ema50"] and slope < 0:
        return "bear"
    return "mixed"


def attribution_rows(trades, market_bars):
    buckets = defaultdict(list)
    for trade in trades:
        timestamp = parse_time(trade["signal_time"])
        regime = regime_at(market_bars, timestamp)
        profile = trade.get("profile") or "unspecified"
        buckets[(regime, profile)].append(trade)
    rows = []
    for (regime, profile), items in sorted(buckets.items()):
        pnl = sum(float(item["pnl"]) for item in items)
        wins = sum(float(item["pnl"]) > 0 for item in items)
        gross_win = sum(max(0, float(item["pnl"])) for item in items)
        gross_loss = abs(sum(min(0, float(item["pnl"])) for item in items))
        rows.append(
            {
                "regime": regime,
                "risk_profile": profile,
                "trades": len(items),
                "wins": wins,
                "win_rate_percent": round(wins / len(items) * 100, 2),
                "net_pnl": round(pnl, 2),
                "profit_factor": round(gross_win / gross_loss, 3)
                if gross_loss
                else None,
            }
        )
    return rows


def load_optional(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--purge-days", type=int, default=1)
    parser.add_argument("--historical-quotes")
    parser.add_argument("--event-calendar")
    parser.add_argument("--estimated-slippage-bps", type=float, default=5.0)
    parser.add_argument("--apply-regime-exposure-bands", action="store_true")
    parser.add_argument("--only-entry-model")
    parser.add_argument(
        "--calibrate-entry-weights",
        action="store_true",
        help="Select a factor-weight profile on each training window only.",
    )
    args = parser.parse_args()

    start = parse_time(args.start)
    end = parse_time(args.end)
    windows = walk_forward_windows(
        start, end, args.train_days, args.test_days, args.purge_days
    )
    if not windows:
        raise ValueError("date range is too short for one walk-forward window")

    load_env()
    data_url = os.environ.get(
        "ALPACA_DATA_URL", "https://data.alpaca.markets/v2"
    ).rstrip("/")
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
    }
    configs = load_strategy_configs("config/watchers.json")
    if args.only_entry_model:
        configs = isolate_entry_model_configs(configs, args.only_entry_model)
    configs = {symbol: configs[symbol] for symbol in args.symbols}
    quote_payload = load_optional(args.historical_quotes)
    event_calendar = load_optional(args.event_calendar)
    configs, filter_coverage = prepare_filter_configs(
        configs,
        has_quotes=quote_payload is not None,
        has_calendar=event_calendar is not None,
    )
    context = HistoricalFilterContext(quote_payload, event_calendar)

    data_start = min(window["train_start"] for window in windows)
    data_end = max(window["test_end"] for window in windows)
    market_bars = fetch_bars(
        "QQQ", data_url, headers, start=data_start, end=data_end
    )
    bars_by_symbol = {}
    for index, symbol in enumerate(args.symbols, 1):
        bars_by_symbol[symbol] = (
            market_bars
            if symbol == "QQQ"
            else fetch_bars(symbol, data_url, headers, start=data_start, end=data_end)
        )
        print(f"DATA {index}/{len(args.symbols)} {symbol}: {len(bars_by_symbol[symbol])} bars")

    benchmark_symbols = {"QQQ"}
    health_symbols = set()
    for symbol, config in configs.items():
        settings = position_health_config(config)
        benchmark = str(
            (settings.get("benchmark_by_symbol") or {}).get(symbol)
            or settings.get("benchmark_symbol")
            or "QQQ"
        ).upper()
        benchmark_symbols.add(benchmark)
        if settings.get("enabled", True) and not settings.get("shadow_mode", True):
            health_symbols.update((symbol, benchmark))
    regime_bars = {"QQQ": market_bars}
    for symbol in sorted(benchmark_symbols - {"QQQ"}):
        regime_bars[symbol] = fetch_bars(
            symbol, data_url, headers, start=data_start, end=data_end
        )
    health_bars = {}
    for symbol in sorted(health_symbols):
        health_bars[symbol] = fetch_bars(
            symbol,
            data_url,
            headers,
            timeframe="1Hour",
            start=data_start,
            end=data_end,
        )

    window_reports = []
    all_trades = []
    aggregate_filter_blockers = defaultdict(int)
    aggregate_signal_diagnostics = defaultdict(int)
    forward_return_reports = []
    stitched_equity_curve = []
    stitched_equity = ACCOUNT_EQUITY
    for number, window in enumerate(windows, 1):
        sliced_symbols = {
            symbol: slice_bars(bars, window["train_start"], window["test_end"])
            for symbol, bars in bars_by_symbol.items()
        }
        sliced_market = slice_bars(
            market_bars, window["train_start"], window["test_end"]
        )
        sliced_regime = {
            symbol: slice_bars(bars, window["train_start"], window["test_end"])
            for symbol, bars in regime_bars.items()
        }
        sliced_health = {
            symbol: slice_bars(bars, window["train_start"], window["test_end"])
            for symbol, bars in health_bars.items()
        }
        window_configs = configs
        calibration = None
        if args.calibrate_entry_weights:
            training_results = []
            training_symbols = {
                symbol: slice_bars(bars, window["train_start"], window["train_end"])
                for symbol, bars in bars_by_symbol.items()
            }
            training_market = slice_bars(
                market_bars, window["train_start"], window["train_end"]
            )
            training_regime = {
                symbol: slice_bars(bars, window["train_start"], window["train_end"])
                for symbol, bars in regime_bars.items()
            }
            training_health = {
                symbol: slice_bars(bars, window["train_start"], window["train_end"])
                for symbol, bars in health_bars.items()
            }
            for profile, weights in WEIGHT_PROFILES.items():
                training_simulation = simulate_portfolio(
                    configs_with_score_weights(configs, weights),
                    training_symbols,
                    training_market,
                    starting_equity=ACCOUNT_EQUITY,
                    health_bars_by_symbol=training_health,
                    regime_bars_by_symbol=training_regime,
                    entry_filter_context_at=context.at,
                    entry_window=(window["train_start"], window["train_end"]),
                    estimated_slippage_bps=args.estimated_slippage_bps,
                    apply_regime_exposure_bands=args.apply_regime_exposure_bands,
                )
                training_results.append({
                    "profile": profile,
                    "weights": weights,
                    "objective": calibration_objective(training_simulation),
                    "trades": len(training_simulation.get("trades_detail") or []),
                    "net_pnl": training_simulation["portfolio"]["net_pnl"],
                    "return_percent": training_simulation["portfolio"]["return_percent"],
                    "max_drawdown_percent": training_simulation["portfolio"]["max_drawdown_percent"],
                })
            selected = select_weight_profile(training_results)
            window_configs = configs_with_score_weights(configs, selected["weights"])
            calibration = {
                "selection_data_end": iso_utc(window["train_end"]),
                "selected_profile": selected["profile"],
                "selected_weights": selected["weights"],
                "training_results": training_results,
            }
        simulation = simulate_portfolio(
            window_configs,
            sliced_symbols,
            sliced_market,
            starting_equity=ACCOUNT_EQUITY,
            health_bars_by_symbol=sliced_health,
            regime_bars_by_symbol=sliced_regime,
            entry_filter_context_at=context.at,
            entry_window=(window["test_start"], window["test_end"]),
            estimated_slippage_bps=args.estimated_slippage_bps,
            apply_regime_exposure_bands=args.apply_regime_exposure_bands,
        )
        trades = simulation["trades_detail"]
        all_trades.extend(trades)
        for counts in simulation["entry_filter_blockers"].values():
            for blocker, count in counts.items():
                aggregate_filter_blockers[blocker] += count
        for key in (
            "candidate_evaluations", "qualified_signals", "selected_signals",
            "rejected_candidates",
        ):
            aggregate_signal_diagnostics[key] += int(
                simulation["signal_diagnostics"].get(key) or 0
            )
        forward_return_reports.append(simulation["rejected_candidate_forward_returns"])
        window_start_equity = float(simulation["portfolio"]["starting_equity"])
        for point in simulation["equity_curve"]:
            stitched_equity_curve.append(
                {
                    **point,
                    "equity": stitched_equity + float(point["equity"]) - window_start_equity,
                }
            )
        stitched_equity += float(simulation["portfolio"]["net_pnl"])
        window_reports.append(
            {
                **{key: iso_utc(value) for key, value in window.items()},
                "portfolio": simulation["portfolio"],
                "baseline_metrics": simulation["baseline_metrics"],
                "attribution": attribution_rows(trades, market_bars),
                "attribution_by_regime_sector_model": attribution_by_dimensions(
                    trades, market_bars, regime_at
                ),
                "entry_filter_blockers": simulation["entry_filter_blockers"],
                "signal_diagnostics": simulation["signal_diagnostics"],
                "rejected_candidate_forward_returns": simulation[
                    "rejected_candidate_forward_returns"
                ],
                "weight_calibration": calibration,
            }
        )
        print(
            f"WINDOW {number}/{len(windows)}: trades={len(trades)} "
            f"pnl={simulation['portfolio']['net_pnl']}"
        )

    aggregate_portfolio = {
        "starting_equity": ACCOUNT_EQUITY,
        "ending_equity": round(stitched_equity, 2),
        "net_pnl": round(stitched_equity - ACCOUNT_EQUITY, 2),
        "return_percent": round((stitched_equity / ACCOUNT_EQUITY - 1) * 100, 4),
        "max_drawdown_percent": max(
            (window["portfolio"]["max_drawdown_percent"] for window in window_reports),
            default=0,
        ),
    }
    aggregate_signal_diagnostics["rejection_rate_percent"] = round(
        100
        * aggregate_signal_diagnostics["rejected_candidates"]
        / aggregate_signal_diagnostics["candidate_evaluations"],
        4,
    ) if aggregate_signal_diagnostics["candidate_evaluations"] else 0
    report = {
        "generated_at": iso_utc(datetime.datetime.now(UTC)),
        "method": (
            "rolling walk-forward; optional factor weights are selected using "
            "training-window results only, frozen, then evaluated on the purged "
            "out-of-sample test window"
        ),
        "symbols": args.symbols,
        "configuration_snapshot": configs,
        "provenance": {
            **source_provenance(ROOT),
            "data_source": {
                "provider": "Alpaca",
                "feed": "iex",
                "adjustment": "raw",
            },
            "historical_quotes_sha256": (
                stable_hash(quote_payload) if quote_payload is not None else None
            ),
            "event_calendar_sha256": (
                stable_hash(event_calendar) if event_calendar is not None else None
            ),
            "config_sha256": stable_hash(configs),
            "walk_forward_parameters": {
                "start": iso_utc(start),
                "end": iso_utc(end),
                "train_days": args.train_days,
                "test_days": args.test_days,
                "purge_days": args.purge_days,
                "estimated_slippage_bps": args.estimated_slippage_bps,
                "apply_regime_exposure_bands": args.apply_regime_exposure_bands,
                "isolated_entry_model": args.only_entry_model,
                "calibrate_entry_weights": args.calibrate_entry_weights,
                "candidate_weight_profiles": WEIGHT_PROFILES,
            },
            "datasets": {
                "entry_5Min": dataset_manifest(bars_by_symbol, "5Min"),
                "regime_5Min": dataset_manifest(regime_bars, "5Min"),
                "position_health_1Hour": dataset_manifest(health_bars, "1Hour"),
            },
        },
        "filter_coverage": filter_coverage,
        "summary": {
            "windows": len(window_reports),
            "profitable_windows": sum(
                window["portfolio"]["net_pnl"] > 0 for window in window_reports
            ),
            "trades": len(all_trades),
            "summed_test_window_pnl": round(
                sum(float(trade["pnl"]) for trade in all_trades), 2
            ),
            "entry_filter_blockers": dict(sorted(aggregate_filter_blockers.items())),
            "signal_diagnostics": dict(aggregate_signal_diagnostics),
        },
        "aggregate_portfolio": aggregate_portfolio,
        "aggregate_baseline_metrics": performance_metrics(
            aggregate_portfolio,
            all_trades,
            stitched_equity_curve,
            estimated_slippage_bps=args.estimated_slippage_bps,
        ),
        "windows": window_reports,
        "aggregate_by_regime_and_risk_profile": attribution_rows(
            all_trades, market_bars
        ),
        "aggregate_by_regime_sector_model": attribution_by_dimensions(
            all_trades, market_bars, regime_at
        ),
        "aggregate_rejected_candidate_forward_returns": combine_forward_return_reports(
            forward_return_reports
        ),
        "trades": all_trades,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"WROTE {output}")


if __name__ == "__main__":
    raise SystemExit(main())
