import datetime
import hashlib
import json
import math
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path


DEFAULT_FORWARD_HORIZONS = (1, 3, 6, 12)
TRADING_DAYS_PER_YEAR = 252


def _round(value, digits=4):
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _json_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda item: item.isoformat() if hasattr(item, "isoformat") else str(item),
    ).encode("utf-8")


def stable_hash(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def dataset_manifest(bars_by_symbol, timeframe="5Min"):
    manifest = {}
    for symbol, bars in sorted((bars_by_symbol or {}).items()):
        compact = [
            [
                bar.get("t"), bar.get("o"), bar.get("h"), bar.get("l"),
                bar.get("c"), bar.get("v"),
            ]
            for bar in bars
        ]
        manifest[symbol] = {
            "timeframe": timeframe,
            "bars": len(bars),
            "start": bars[0]["t"].isoformat() if bars else None,
            "end": bars[-1]["t"].isoformat() if bars else None,
            "sha256": stable_hash(compact),
        }
    return manifest


def source_provenance(root):
    root = Path(root)

    def git(*args):
        try:
            return subprocess.run(
                ["git", *args],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {
        "git_commit": commit,
        "git_dirty": bool(status) if status is not None else None,
    }


def _daily_equity(equity_curve):
    closes = {}
    for point in equity_curve or []:
        timestamp = datetime.datetime.fromisoformat(
            str(point["timestamp"]).replace("Z", "+00:00")
        )
        closes[timestamp.date()] = float(point["equity"])
    return [closes[day] for day in sorted(closes)]


def _returns(values):
    return [current / previous - 1 for previous, current in zip(values, values[1:]) if previous]


def _trade_notional(trade):
    notional = abs(float(trade.get("initial_notional") or 0))
    for item in trade.get("adds") or []:
        notional += abs(float(item.get("qty") or 0) * float(item.get("price") or 0))
    for item in trade.get("reductions") or []:
        notional += abs(float(item.get("qty") or 0) * float(item.get("price") or 0))
    notional += abs(float(trade.get("qty") or 0) * float(trade.get("exit_price") or 0))
    return notional


def performance_metrics(
    portfolio,
    trades,
    equity_curve,
    *,
    estimated_slippage_bps=5.0,
):
    trades = trades or []
    equity_curve = equity_curve or []
    starting_equity = float(portfolio.get("starting_equity") or 0)
    ending_equity = float(portfolio.get("ending_equity") or starting_equity)
    daily_equity = _daily_equity(equity_curve)
    daily_returns = _returns(daily_equity)
    mean_return = statistics.fmean(daily_returns) if daily_returns else None
    return_stdev = statistics.stdev(daily_returns) if len(daily_returns) >= 2 else None
    downside = [min(0.0, value) for value in daily_returns]
    downside_deviation = (
        math.sqrt(statistics.fmean(value * value for value in downside))
        if downside and any(value < 0 for value in downside)
        else None
    )
    first_time = equity_curve[0].get("timestamp") if equity_curve else None
    last_time = equity_curve[-1].get("timestamp") if equity_curve else None
    elapsed_years = None
    if first_time and last_time:
        first = datetime.datetime.fromisoformat(str(first_time).replace("Z", "+00:00"))
        last = datetime.datetime.fromisoformat(str(last_time).replace("Z", "+00:00"))
        elapsed_years = max(0.0, (last - first).total_seconds() / (365.2425 * 86400))
    cagr = (
        (ending_equity / starting_equity) ** (1 / elapsed_years) - 1
        if starting_equity > 0 and ending_equity > 0 and elapsed_years
        else None
    )

    pnls = [float(trade.get("pnl") or 0) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    turnover = sum(_trade_notional(trade) for trade in trades)
    slippage_cost = turnover * float(estimated_slippage_bps) / 10000
    exposures = [float(point.get("gross_exposure_percent") or 0) for point in equity_curve]
    cash_exposures = [float(point.get("cash_percent") or 0) for point in equity_curve]

    def exposure_share(threshold):
        return (
            100 * sum(value >= threshold for value in exposures) / len(exposures)
            if exposures
            else 0
        )

    return {
        "cagr_percent": _round(cagr * 100 if cagr is not None else None),
        "total_return_percent": portfolio.get("return_percent"),
        "maximum_drawdown_percent": portfolio.get("max_drawdown_percent"),
        "sharpe_ratio": _round(
            mean_return / return_stdev * math.sqrt(TRADING_DAYS_PER_YEAR)
            if mean_return is not None and return_stdev
            else None
        ),
        "sortino_ratio": _round(
            mean_return / downside_deviation * math.sqrt(TRADING_DAYS_PER_YEAR)
            if mean_return is not None and downside_deviation
            else None
        ),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": _round(100 * len(wins) / len(trades) if trades else 0),
        "average_win": _round(statistics.fmean(wins) if wins else None, 2),
        "average_loss": _round(statistics.fmean(losses) if losses else None, 2),
        "expectancy_per_trade": _round(statistics.fmean(pnls) if pnls else 0, 2),
        "profit_factor": _round(gross_profit / gross_loss if gross_loss else None),
        "gross_turnover": _round(turnover, 2),
        "turnover_multiple_of_starting_equity": _round(
            turnover / starting_equity if starting_equity else None
        ),
        "estimated_slippage_bps": float(estimated_slippage_bps),
        "estimated_slippage_cost": _round(slippage_cost, 2),
        "net_pnl_after_estimated_slippage": _round(
            float(portfolio.get("net_pnl") or 0) - slippage_cost, 2
        ),
        "average_gross_exposure_percent": _round(
            statistics.fmean(exposures) if exposures else 0
        ),
        "average_cash_percent": _round(
            statistics.fmean(cash_exposures) if cash_exposures else 0
        ),
        "time_at_or_above_25_percent_exposure": _round(exposure_share(25)),
        "time_at_or_above_50_percent_exposure": _round(exposure_share(50)),
        "time_at_or_above_75_percent_exposure": _round(exposure_share(75)),
        "daily_return_observations": len(daily_returns),
    }


def new_forward_return_accumulator(horizons=DEFAULT_FORWARD_HORIZONS):
    return {
        "horizons": tuple(int(value) for value in horizons),
        "total_candidates": 0,
        "groups": defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0, 0.0])),
    }


def _score_bucket(score):
    if score is None:
        return "unavailable"
    lower = int(float(score) // 10 * 10)
    return f"{lower:02d}-{min(100, lower + 9):02d}"


def record_rejected_candidate(accumulator, candidate, symbol, bars, index):
    if candidate.get("status") == "active_signal" or index >= len(bars):
        return
    base_price = float(bars[index]["c"])
    if base_price <= 0:
        return
    accumulator["total_candidates"] += 1
    model = candidate.get("model_id") or "unknown"
    blockers = sorted(set(candidate.get("blockers") or []))
    groups = ["overall", f"model:{model}", f"score:{_score_bucket(candidate.get('setup_score'))}"]
    groups.extend(f"blocker:{blocker}" for blocker in blockers)
    for horizon in accumulator["horizons"]:
        future_index = index + horizon
        if future_index >= len(bars):
            continue
        forward_return = float(bars[future_index]["c"]) / base_price - 1
        for group in groups:
            stats = accumulator["groups"][group][horizon]
            stats[0] += 1
            stats[1] += forward_return
            stats[2] += forward_return > 0
            stats[3] += forward_return * forward_return


def finalize_forward_returns(accumulator):
    rows = []
    for group, horizons in sorted(accumulator["groups"].items()):
        for horizon, (count, total, positive, total_squares) in sorted(horizons.items()):
            mean = total / count if count else 0
            variance = max(0.0, total_squares / count - mean * mean) if count else 0
            rows.append(
                {
                    "group": group,
                    "horizon_bars": horizon,
                    "observations": count,
                    "average_forward_return_percent": _round(mean * 100),
                    "positive_forward_return_percent": _round(100 * positive / count if count else 0),
                    "forward_return_stdev_percent": _round(math.sqrt(variance) * 100),
                }
            )
    return {
        "method": "diagnostic only; future closes are evaluated after the simulated decision and never enter order logic",
        "rejected_candidates": accumulator["total_candidates"],
        "horizons_bars": list(accumulator["horizons"]),
        "rows": rows,
    }


def combine_forward_return_reports(reports):
    combined = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    total_candidates = 0
    horizons = set()
    for report in reports or []:
        total_candidates += int(report.get("rejected_candidates") or 0)
        horizons.update(report.get("horizons_bars") or [])
        for row in report.get("rows") or []:
            count = int(row.get("observations") or 0)
            if not count:
                continue
            mean = float(row.get("average_forward_return_percent") or 0) / 100
            stdev = float(row.get("forward_return_stdev_percent") or 0) / 100
            positive_rate = float(row.get("positive_forward_return_percent") or 0) / 100
            item = combined[(row["group"], int(row["horizon_bars"]))]
            item[0] += count
            item[1] += mean * count
            item[2] += positive_rate * count
            item[3] += (stdev * stdev + mean * mean) * count
    rows = []
    for (group, horizon), (count, total, positive, total_squares) in sorted(combined.items()):
        mean = total / count
        variance = max(0.0, total_squares / count - mean * mean)
        rows.append(
            {
                "group": group,
                "horizon_bars": horizon,
                "observations": count,
                "average_forward_return_percent": _round(mean * 100),
                "positive_forward_return_percent": _round(positive / count * 100),
                "forward_return_stdev_percent": _round(math.sqrt(variance) * 100),
            }
        )
    return {
        "method": "aggregate of out-of-sample window diagnostics; future closes never enter order logic",
        "rejected_candidates": total_candidates,
        "horizons_bars": sorted(horizons),
        "rows": rows,
    }


def attribution_by_dimensions(trades, market_bars, regime_at):
    buckets = defaultdict(list)
    for trade in trades or []:
        timestamp = datetime.datetime.fromisoformat(
            str(trade["signal_time"]).replace("Z", "+00:00")
        )
        key = (
            regime_at(market_bars, timestamp),
            trade.get("sector_benchmark") or "unmapped",
            trade.get("entry_model_id") or "unknown",
        )
        buckets[key].append(trade)
    rows = []
    for (regime, sector, model), items in sorted(buckets.items()):
        pnls = [float(item.get("pnl") or 0) for item in items]
        gross_profit = sum(value for value in pnls if value > 0)
        gross_loss = -sum(value for value in pnls if value < 0)
        rows.append(
            {
                "regime": regime,
                "sector_benchmark": sector,
                "model": model,
                "trades": len(items),
                "win_rate_percent": _round(100 * sum(value > 0 for value in pnls) / len(items)),
                "net_pnl": _round(sum(pnls), 2),
                "expectancy": _round(statistics.fmean(pnls), 2),
                "profit_factor": _round(gross_profit / gross_loss if gross_loss else None),
            }
        )
    return rows


__all__ = [
    "DEFAULT_FORWARD_HORIZONS",
    "attribution_by_dimensions",
    "combine_forward_return_reports",
    "dataset_manifest",
    "finalize_forward_returns",
    "new_forward_return_accumulator",
    "performance_metrics",
    "record_rejected_candidate",
    "source_provenance",
    "stable_hash",
]
