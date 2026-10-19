"""Export recorded watcher fills and completed bars for an external LLM review.

Uses only local reports and market-data snapshots. No broker requests are made.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import json
import statistics
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
UTC = dt.timezone.utc
EASTERN = ZoneInfo("America/New_York")
BAR_DURATION = dt.timedelta(minutes=5)


def timestamp(value):
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def utc_text(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def percent(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return round((numerator / denominator - 1) * 100, 4)


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")


def watcher_sources(watchers_file):
    payload = json.loads(watchers_file.read_text(encoding="utf-8"))
    watchers = [*payload.get("managed_watchers", []), *payload.get("new_watchers", [])]
    symbols = {row["symbol"] for row in watchers}
    groups = {row["symbol"]: "managed" for row in payload.get("managed_watchers", [])}
    groups.update({row["symbol"]: "new_candidate" for row in payload.get("new_watchers", [])})
    attribution = {}
    for row in watchers:
        state_path = ROOT / row["state"]
        if not state_path.exists():
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for order_id, intent in (state.get("exit_order_intents") or {}).items():
            if intent.get("exit_reason") not in (None, "unknown"):
                attribution[order_id] = {
                    "exit_reason": intent["exit_reason"],
                    "exit_reason_source": "watcher_intent",
                }
        for episode in state.get("closed_position_episodes") or []:
            for event in episode.get("exit_events") or []:
                order_id = event.get("order_id")
                if order_id and event.get("exit_reason") not in (None, "unknown"):
                    attribution.setdefault(order_id, {
                        "exit_reason": event["exit_reason"],
                        "exit_reason_source": "watcher_episode",
                    })
    return symbols, groups, attribution


def load_ledger(reports_dir, symbols, groups, attribution):
    report_paths = sorted(reports_dir.glob("daily_report_*.json"))
    if not report_paths:
        raise ValueError(f"No daily JSON reports in {reports_dir}")
    fills_by_key = {}
    orders_by_key = {}
    latest_report = None
    for path in report_paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        latest_report = report
        report_date = report.get("report_date") or path.stem[-10:]
        for field, side in (("stocks_bought", "buy"), ("stocks_sold", "sell")):
            for item in report.get(field) or []:
                symbol = item.get("symbol")
                when = timestamp(item.get("timestamp"))
                if symbol not in symbols or when is None:
                    continue
                qty, price = number(item.get("qty")), number(item.get("price"))
                if qty is None or price is None:
                    continue
                key = (item.get("order_id"), symbol, side, utc_text(when), qty, price)
                if key in fills_by_key:
                    fills_by_key[key]["source_reports"].append(path.name)
                    continue
                reason = attribution.get(item.get("order_id")) or {}
                fills_by_key[key] = {
                    "symbol": symbol,
                    "watcher_group": groups[symbol],
                    "side": side,
                    "time_utc": utc_text(when),
                    "time_et": when.astimezone(EASTERN).isoformat(),
                    "session_date_et": when.astimezone(EASTERN).date().isoformat(),
                    "qty": qty,
                    "price": price,
                    "order_id": item.get("order_id"),
                    "cost_basis": number(item.get("cost_basis")),
                    "avg_entry_price_reported": number(item.get("avg_entry_price")),
                    "realized_pl_reported": number(item.get("realized_pl")),
                    "exit_reason": reason.get("exit_reason"),
                    "exit_reason_source": reason.get("exit_reason_source"),
                    "source_reports": [path.name],
                    "source_type": "broker_fill_in_daily_report",
                }
        for item in report.get("bot_buy_orders_submitted") or []:
            symbol = item.get("symbol")
            if symbol not in symbols:
                continue
            key = (item.get("order_id"), item.get("timestamp"))
            orders_by_key.setdefault(key, {
                **item,
                "watcher_group": groups[symbol],
                "source_report": path.name,
                "source_type": "bot_buy_order_submission_in_daily_report",
            })
    fills = sorted(fills_by_key.values(), key=lambda row: (row["time_utc"], row["symbol"], row["side"]))
    orders = sorted(orders_by_key.values(), key=lambda row: (row.get("timestamp") or "", row["symbol"]))
    order_reasons = {row.get("order_id"): row.get("reason") for row in orders if row.get("order_id")}
    for fill in fills:
        if fill["side"] == "buy":
            fill["bot_entry_reason"] = order_reasons.get(fill["order_id"])
    positions = []
    for row in (latest_report or {}).get("current_positions") or []:
        if row.get("symbol") not in symbols:
            continue
        keep = {key: value for key, value in row.items() if key.startswith("position_health_")}
        keep.update({key: row.get(key) for key in (
            "symbol", "qty", "avg_entry_price", "current_price", "market_value",
            "total_gain_loss", "total_gain_loss_percent", "entry_setup_score",
        )})
        keep["as_of_report"] = (latest_report or {}).get("report_date")
        positions.append(keep)
    return report_paths, fills, orders, positions


def load_completed_bars(bars_root, symbols, groups):
    selected = {}
    source_paths = []
    excluded_unfinished = 0
    for source in ("alpaca", "tastytrade"):
        for path in sorted((bars_root / source).glob("*.jsonl")):
            source_paths.append(path)
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        snapshot = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    symbol = snapshot.get("symbol")
                    if symbol not in symbols or snapshot.get("timeframe") != "5Min":
                        continue
                    observed = timestamp(snapshot.get("observed_at") or snapshot.get("end"))
                    if observed is None:
                        continue
                    for bar in snapshot.get("bars") or []:
                        start = timestamp(bar.get("t"))
                        if start is None:
                            continue
                        if start + BAR_DURATION > observed:
                            excluded_unfinished += 1
                            continue
                        key = (source, symbol, utc_text(start))
                        previous = selected.get(key)
                        if previous is not None and observed < previous["_observed"]:
                            previous["snapshot_count"] += 1
                            continue
                        local = start.astimezone(EASTERN)
                        price_fields = {name: number(bar.get(short)) for name, short in (
                            ("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"),
                            ("volume", "v"), ("vwap", "vw"),
                        )}
                        flags = []
                        o, h, l, c = (price_fields[name] for name in ("open", "high", "low", "close"))
                        if any(value is None or value <= 0 for value in (o, h, l, c)):
                            flags.append("missing_or_nonpositive_ohlc")
                        elif h < max(o, c, l) or l > min(o, c, h):
                            flags.append("ohlc_inconsistent")
                        if price_fields["volume"] is None or price_fields["volume"] < 0:
                            flags.append("missing_or_negative_volume")
                        regular = dt.time(9, 30) <= local.time() < dt.time(16, 0) and local.weekday() < 5
                        selected[key] = {
                            "source": source,
                            "feed": (snapshot.get("quote") or {}).get("feed") or bar.get("feed"),
                            "symbol": symbol,
                            "watcher_group": groups[symbol],
                            "timeframe": "5Min",
                            "start_utc": utc_text(start),
                            "end_utc": utc_text(start + BAR_DURATION),
                            "start_et": local.isoformat(),
                            "session_date_et": local.date().isoformat(),
                            "regular_session": regular,
                            **price_fields,
                            "trade_count": bar.get("n"),
                            "quality_flags": flags,
                            "first_observed_at_utc": previous["first_observed_at_utc"] if previous else utc_text(observed),
                            "last_observed_at_utc": utc_text(observed),
                            "snapshot_count": previous["snapshot_count"] + 1 if previous else 1,
                            "_observed": observed,
                        }
    bars = sorted(selected.values(), key=lambda row: (row["source"], row["symbol"], row["start_utc"]))
    for row in bars:
        del row["_observed"]
    return source_paths, bars, excluded_unfinished


def fill_context(fills, bars):
    by_symbol = collections.defaultdict(list)
    covered_sessions = collections.defaultdict(set)
    for bar in bars:
        if bar["source"] == "alpaca" and bar["regular_session"] and not bar["quality_flags"]:
            by_symbol[bar["symbol"]].append(bar)
            covered_sessions[bar["symbol"]].add(bar["session_date_et"])
    for items in by_symbol.values():
        items.sort(key=lambda row: row["start_utc"])
    contexts = []
    for fill in fills:
        if fill["session_date_et"] not in covered_sessions[fill["symbol"]]:
            continue
        items = by_symbol.get(fill["symbol"], [])
        if not items:
            continue
        when = fill["time_utc"]
        starts = [row["start_utc"] for row in items]
        ends = [row["end_utc"] for row in items]
        prior_index = bisect.bisect_right(ends, when)
        future_index = bisect.bisect_left(starts, when)
        prior = items[max(0, prior_index - 20):prior_index]
        future = items[future_index:future_index + 20]
        if not prior and not future:
            continue
        context = {
            "symbol": fill["symbol"],
            "side": fill["side"],
            "fill_time_utc": when,
            "fill_price": fill["price"],
            "fill_qty": fill["qty"],
            "order_id": fill["order_id"],
            "bar_source": "alpaca_iex_recorded_5min",
            "prior_completed_bar_count": len(prior),
            "prior_20_high": max((row["high"] for row in prior), default=None),
            "prior_20_low": min((row["low"] for row in prior), default=None),
            "prior_bar_end_utc": prior[-1]["end_utc"] if prior else None,
            "prior_bar_close": prior[-1]["close"] if prior else None,
            "fill_vs_prior_close_pct": percent(fill["price"], prior[-1]["close"]) if prior else None,
            "future_recorded_bar_count": len(future),
            "first_future_bar_start_utc": future[0]["start_utc"] if future else None,
            "after_5_recorded_bars_close_utc": future[4]["end_utc"] if len(future) >= 5 else None,
            "after_5_recorded_bars_close_return_pct": percent(future[4]["close"], fill["price"]) if len(future) >= 5 else None,
            "after_20_recorded_bars_close_utc": future[19]["end_utc"] if len(future) >= 20 else None,
            "after_20_recorded_bars_close_return_pct": percent(future[19]["close"], fill["price"]) if len(future) >= 20 else None,
            "next_20_recorded_bars_peak_pct": percent(max(row["high"] for row in future), fill["price"]) if len(future) >= 20 else None,
            "next_20_recorded_bars_trough_pct": percent(min(row["low"] for row in future), fill["price"]) if len(future) >= 20 else None,
        }
        contexts.append(context)
    return contexts


def daily_bar_summaries(bars, fills):
    by_day = collections.defaultdict(list)
    all_primary = collections.defaultdict(list)
    for bar in bars:
        if bar["source"] != "alpaca" or not bar["regular_session"] or bar["quality_flags"]:
            continue
        by_day[(bar["symbol"], bar["session_date_et"])].append(bar)
        all_primary[bar["symbol"]].append(bar)
    fill_days = collections.defaultdict(list)
    for fill in fills:
        fill_days[(fill["symbol"], fill["session_date_et"])].append(fill)
    summaries = []
    for symbol, items in all_primary.items():
        items.sort(key=lambda row: row["start_utc"])
        indexes = {row["start_utc"]: index for index, row in enumerate(items)}
        for (day_symbol, day), day_bars in by_day.items():
            if day_symbol != symbol:
                continue
            day_bars.sort(key=lambda row: row["start_utc"])
            first, last = day_bars[0], day_bars[-1]
            prior = items[max(0, indexes[first["start_utc"]] - 20):indexes[first["start_utc"]]]
            first_hour = [row for row in day_bars if row["start_et"][11:16] < "10:30"]
            day_fills = fill_days.get((symbol, day), [])
            sell_pl = [row["realized_pl_reported"] for row in day_fills if row["side"] == "sell" and row["realized_pl_reported"] is not None]
            opening_available = first["start_et"][11:16] == "09:30"
            summaries.append({
                "symbol": symbol,
                "watcher_group": first["watcher_group"],
                "session_date_et": day,
                "bar_source": "alpaca_iex_recorded_5min",
                "regular_session_completed_bar_count": len(day_bars),
                "regular_session_expected_slots_full_day": 78,
                "opening_0930_bar_available": opening_available,
                "first_bar_start_et": first["start_et"],
                "last_bar_start_et": last["start_et"],
                "first_bar_close": first["close"],
                "last_bar_close": last["close"],
                "first_to_last_close_return_pct": percent(last["close"], first["close"]),
                "session_high_recorded": max(row["high"] for row in day_bars),
                "session_low_recorded": min(row["low"] for row in day_bars),
                "first_hour_last_close": first_hour[-1]["close"] if first_hour else None,
                "first_hour_close_return_pct": percent(first_hour[-1]["close"], first["close"]) if first_hour else None,
                "opening_prior_completed_bar_count": len(prior) if opening_available else None,
                "opening_prior_20_high": max(row["high"] for row in prior) if opening_available and len(prior) == 20 else None,
                "opening_close_above_prior_20_high": first["close"] > max(row["high"] for row in prior) if opening_available and len(prior) == 20 else None,
                "buy_fill_count": sum(row["side"] == "buy" for row in day_fills),
                "sell_fill_count": sum(row["side"] == "sell" for row in day_fills),
                "sell_fill_count_with_reported_pl": len(sell_pl),
                "reported_realized_pl_sum": round(sum(sell_pl), 4) if sell_pl else None,
            })
    return sorted(summaries, key=lambda row: (row["session_date_et"], row["symbol"]))


def review_text(manifest, fills, contexts, day_rows):
    known_sells = [row for row in fills if row["side"] == "sell" and row["realized_pl_reported"] is not None]
    sorted_sells = sorted(known_sells, key=lambda row: row["realized_pl_reported"])
    buy_follow = [row["after_20_recorded_bars_close_return_pct"] for row in contexts
                  if row["side"] == "buy" and row["after_20_recorded_bars_close_return_pct"] is not None]
    opening = [row for row in day_rows if row["opening_close_above_prior_20_high"] is True]
    lines = [
        "# TraderBot performance data review",
        "",
        f"Generated from local records through {manifest['as_of_report_date']}.",
        "",
        "## Coverage",
        "",
        f"- Watched symbols: {manifest['watched_symbol_count']}.",
        f"- Daily reports: {manifest['daily_report_count']} ({manifest['report_date_first']} through {manifest['report_date_last']}).",
        f"- Broker fills in those reports: {manifest['fill_count']} ({manifest['buy_fill_count']} buys, {manifest['sell_fill_count']} sells).",
        f"- Sells with broker-reported realized P/L: {len(known_sells)}; reported sum ${sum(row['realized_pl_reported'] for row in known_sells):,.2f}. Other sells have unknown realized P/L in these reports.",
        f"- Sells with a watcher-attributed exit reason: {manifest['sell_fills_with_attributed_exit_reason']}. Remaining reasons are unresolved in the local watcher states.",
        f"- Deduplicated completed 5-minute bars: {manifest['completed_bar_counts_by_source']}. Earliest bar {manifest['bar_date_first']}; latest bar {manifest['bar_date_last']}.",
        f"- Alpaca bars cover {manifest['bar_date_ranges_by_source']['alpaca']['first']} through {manifest['bar_date_ranges_by_source']['alpaca']['last']}; tastytrade includes older historical bars first captured in the September-October snapshot files.",
        f"- Bar quality flags: {manifest['bar_quality_flags_by_source']}. Alpaca regular-session symbol-days contain at most {manifest['max_alpaca_regular_bars_per_symbol_day']} of 78 possible 5-minute slots in this capture.",
        f"- Alpaca IEX symbol-days with an opening close above the preceding 20 recorded bars: {len(opening)}. This is a price event, not an order qualification.",
        f"- Buy fills with 20 later recorded Alpaca bars: {len(buy_follow)}. Median bar-close change from fill price: {statistics.median(buy_follow):.2f}%." if buy_follow else "- No buy fills have 20 later recorded Alpaca bars.",
        "",
        "## Largest broker-reported realized losses on individual sell fills",
        "",
    ]
    for row in sorted_sells[:10]:
        lines.append(f"- {row['time_et']}: {row['symbol']} {row['qty']:g} shares at ${row['price']:g}; reported P/L ${row['realized_pl_reported']:,.2f}; reason {row['exit_reason'] or 'unresolved'}.")
    lines.extend(["", "## Largest broker-reported realized gains on individual sell fills", ""])
    for row in reversed(sorted_sells[-10:]):
        lines.append(f"- {row['time_et']}: {row['symbol']} {row['qty']:g} shares at ${row['price']:g}; reported P/L ${row['realized_pl_reported']:,.2f}; reason {row['exit_reason'] or 'unresolved'}.")
    lines.extend([
        "", "## Files", "",
        "- `fills.jsonl`: normalized broker fills from daily reports, including available realized P/L and watcher exit attribution.",
        "- `bot_buy_orders.jsonl`: bot buy submissions recorded in daily reports; submitted orders may be canceled or unfilled.",
        "- `completed_alpaca_5m_bars.jsonl` and `completed_tastytrade_5m_bars.jsonl`: one record per completed bar, symbol, and feed; later snapshots replace earlier versions of that same bar.",
        "- `fill_bar_context.jsonl`: prior 20 completed bars and subsequent recorded-bar outcome measures for fills with Alpaca coverage.",
        "- `symbol_days.jsonl`: compact daily market and fill summaries, including opening 20-bar crosses when history is available.",
        "- `open_positions.jsonl`: positions from the latest daily report; unrealized P/L is separate from realized P/L.",
        "- `manifest.json`: counts, source files, and definitions.",
        "- `REVIEW_PROMPT.md`: questions and guardrails for an LLM reviewer.",
        "", "## Interpretation limits", "",
        "Daily fills begin before the recorded Alpaca 5-minute bars, so many fills have no bar context. Historical tastytrade bars begin earlier than their snapshot capture dates; `first_observed_at_utc` shows when each was first recorded. The feeds are Alpaca IEX and tastytrade dxlink; volume, spreads, and even OHLC can differ. Missing bars are left missing. Subsequent bar returns are market moves, not executable trade returns. A sell fill's broker-reported P/L may reflect an entry before the exported window. The report-derived ledger is not an independent broker account export. The smaller core ZIP omits the tastytrade raw bars; use the full ZIP for cross-feed review.",
        "",
    ])
    return "\n".join(lines)


PROMPT = """# Prompt for an LLM reviewer

Review this TraderBot data package. Start with `README.md` and `manifest.json`, then use the JSONL files as needed. The core ZIP has the Alpaca primary-feed bars; the full ZIP also has tastytrade bars.

1. Quantify realized performance from broker-reported sell-fill P/L. State the number of fills with and without P/L and keep open-position gains separate.
2. Examine entry timing with `fills.jsonl`, `fill_bar_context.jsonl`, and `completed_alpaca_5m_bars.jsonl`. Separate weak signals, poor execution, and missing data. Compare the bot's fills with later bar movement without assuming the future high was tradable.
3. Examine exits by reason where attribution exists. Identify cases where a sell was followed by a recovery and cases where a sell avoided further loss. State when the entry was outside bar coverage.
4. Analyze opening breakouts using `symbol_days.jsonl` and the completed bars. A close above the prior 20 recorded bars is only a price event; do not assume the bot was flat, authorized, or had a fillable quote.
5. If `completed_tastytrade_5m_bars.jsonl` is included, compare Alpaca IEX and tastytrade bars by symbol and timestamp before drawing conclusions from volume or a single large move. Exclude rows with nonempty `quality_flags` from price comparisons. Point out feed disagreements and missing 5-minute slots.
6. Give the five most important findings with counts, symbols, dates, and the exact data fields supporting each one. Rank proposed changes by expected value and risk. Mark hypotheses that require a backtest or broker-order history.

Use Eastern time for narrative dates and UTC timestamps to join files. Treat missing values as unknown. Do not claim a rule change improves profit from this observational dataset alone.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchers", type=Path, default=ROOT / "config/watchers.json")
    parser.add_argument("--reports-dir", type=Path, default=ROOT / "runtime/reports/daily")
    parser.add_argument("--bars-root", type=Path, default=ROOT / "runtime/market_data")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    symbols, groups, attribution = watcher_sources(args.watchers)
    report_paths, fills, orders, positions = load_ledger(args.reports_dir, symbols, groups, attribution)
    source_paths, bars, excluded_unfinished = load_completed_bars(args.bars_root, symbols, groups)
    contexts = fill_context(fills, bars)
    day_rows = daily_bar_summaries(bars, fills)
    last_report_date = report_paths[-1].stem[-10:]
    output_dir = args.output_dir or ROOT / "runtime/reports" / f"llm_performance_{last_report_date}"
    output_dir.mkdir(parents=True, exist_ok=True)
    bar_counts = dict(collections.Counter(row["source"] for row in bars))
    bar_date_ranges = {
        source: {
            "first": min((row["session_date_et"] for row in bars if row["source"] == source), default=None),
            "last": max((row["session_date_et"] for row in bars if row["source"] == source), default=None),
        }
        for source in ("alpaca", "tastytrade")
    }
    quality_flags = {
        source: dict(collections.Counter(flag for row in bars if row["source"] == source for flag in row["quality_flags"]))
        for source in ("alpaca", "tastytrade")
    }
    manifest = {
        "schema_version": 1,
        "generated_at_utc": utc_text(dt.datetime.now(UTC)),
        "as_of_report_date": last_report_date,
        "watched_symbol_count": len(symbols),
        "watched_symbols": sorted(symbols),
        "daily_report_count": len(report_paths),
        "report_date_first": report_paths[0].stem[-10:],
        "report_date_last": last_report_date,
        "report_files": [str(path.relative_to(ROOT)) for path in report_paths],
        "fill_count": len(fills),
        "buy_fill_count": sum(row["side"] == "buy" for row in fills),
        "sell_fill_count": sum(row["side"] == "sell" for row in fills),
        "sell_fills_with_reported_realized_pl": sum(row["side"] == "sell" and row["realized_pl_reported"] is not None for row in fills),
        "sell_fills_with_attributed_exit_reason": sum(row["side"] == "sell" and row["exit_reason"] is not None for row in fills),
        "bot_buy_order_record_count": len(orders),
        "open_position_count": len(positions),
        "completed_bar_counts_by_source": bar_counts,
        "bar_quality_flags_by_source": quality_flags,
        "bar_date_ranges_by_source": bar_date_ranges,
        "max_alpaca_regular_bars_per_symbol_day": max((row["regular_session_completed_bar_count"] for row in day_rows), default=0),
        "bar_date_first": min((row["session_date_et"] for row in bars), default=None),
        "bar_date_last": max((row["session_date_et"] for row in bars), default=None),
        "bar_snapshot_files": [str(path.relative_to(ROOT)) for path in source_paths],
        "unfinished_bar_observations_excluded": excluded_unfinished,
        "fill_context_count": len(contexts),
        "symbol_day_count": len(day_rows),
        "bar_deduplication": "Latest observed completed snapshot per source/symbol/bar start; snapshot_count records repeated observations.",
        "regular_session_definition": "Weekdays 09:30 <= bar start < 16:00 America/New_York; holidays and early closes are not separately inferred.",
        "fill_context_definition": "Only fills whose Eastern session has an Alpaca regular-session bar. Future-bar measures start at or after fill time, exclude an overlapping partly elapsed bar, and may span sessions if recorded bars are missing.",
        "realized_pl_definition": "Sum only broker-reported realized_pl fields on sell fills; missing values remain null.",
    }
    write_jsonl(output_dir / "fills.jsonl", fills)
    write_jsonl(output_dir / "bot_buy_orders.jsonl", orders)
    write_jsonl(output_dir / "open_positions.jsonl", positions)
    old_combined = output_dir / "completed_5m_bars.jsonl"
    if old_combined.exists():
        old_combined.unlink()
    write_jsonl(output_dir / "completed_alpaca_5m_bars.jsonl", (row for row in bars if row["source"] == "alpaca"))
    write_jsonl(output_dir / "completed_tastytrade_5m_bars.jsonl", (row for row in bars if row["source"] == "tastytrade"))
    write_jsonl(output_dir / "fill_bar_context.jsonl", contexts)
    write_jsonl(output_dir / "symbol_days.jsonl", day_rows)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "README.md").write_text(review_text(manifest, fills, contexts, day_rows), encoding="utf-8")
    (output_dir / "REVIEW_PROMPT.md").write_text(PROMPT, encoding="utf-8")
    zip_path = output_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(output_dir.iterdir()):
            archive.write(path, arcname=path.name)
    core_zip_path = output_dir.with_name(output_dir.name + "_core").with_suffix(".zip")
    with zipfile.ZipFile(core_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(output_dir.iterdir()):
            if path.name != "completed_tastytrade_5m_bars.jsonl":
                archive.write(path, arcname=path.name)
    print(json.dumps({
        "output_dir": str(output_dir),
        "zip": str(zip_path),
        "core_zip": str(core_zip_path),
        "watched_symbols": len(symbols),
        "fills": len(fills),
        "completed_bars": bar_counts,
        "fill_contexts": len(contexts),
        "symbol_days": len(day_rows),
    }, indent=2))


if __name__ == "__main__":
    main()
