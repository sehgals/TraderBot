import argparse
import datetime
import json
from collections import defaultdict
from pathlib import Path

from traderbot.core_strategy_engine.engine import AlpacaClient, load_env, load_json, save_json


DEFAULT_WATCHERS_PATH = "config/watchers.json"
DEFAULT_REPORT_DIR = "runtime/reports/daily"
LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo
BUY_STATUSES = {
    "dynamic_reentry_order_submitted",
    "reentry_order_submitted",
}
BLOCKED_STATUSES = {
    "dynamic_entry_cash_reserve_blocked",
    "dynamic_entry_order_canceled_cash_reserve",
    "reentry_cash_reserve_blocked",
}


def parse_time(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_utc(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def last_closed_trading_day(now=None):
    current = (now or datetime.datetime.now(LOCAL_TZ)).date()
    if current.weekday() >= 5:
        current -= datetime.timedelta(days=current.weekday() - 4)
    elif datetime.datetime.now(LOCAL_TZ).time() < datetime.time(16, 0):
        current -= datetime.timedelta(days=1)
    while current.weekday() >= 5:
        current -= datetime.timedelta(days=1)
    return current


def day_window(report_date):
    start = datetime.datetime.combine(report_date, datetime.time(0, 0), tzinfo=LOCAL_TZ)
    end = start + datetime.timedelta(days=1)
    return start, end


def iter_watcher_log_records(log_path, start_at, end_at):
    if not log_path.exists():
        return
    with log_path.open("r", encoding="utf-8") as file:
        for line in file:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = parse_time(record.get("timestamp"))
            if not timestamp:
                continue
            timestamp = timestamp.astimezone(LOCAL_TZ)
            if start_at <= timestamp < end_at:
                yield record


def configured_watchers(project_root, watchers_path):
    config = load_json(watchers_path, {})
    watchers = []
    watchers.extend(config.get("managed_watchers", config.get("watchers", [])))
    watchers.extend(config.get("new_watchers", []))
    for watcher in watchers:
        log_path = Path(watcher.get("log", ""))
        if not log_path.is_absolute():
            log_path = project_root / log_path
        yield {**watcher, "log_path": log_path}


def cash_blocked_detail(record, result):
    sizing = result.get("sizing") or {}
    plan = result.get("dynamic_plan") or {}
    return {
        "timestamp": record.get("timestamp"),
        "symbol": record.get("symbol") or result.get("symbol") or plan.get("symbol"),
        "status": result.get("status"),
        "reason": result.get("reason"),
        "mode": result.get("mode") or plan.get("mode"),
        "limit_price": result.get("limit_price") or plan.get("limit_price"),
        "target_notional": result.get("target_notional"),
        "requested_qty": sizing.get("requested_qty"),
        "available_notional": sizing.get("available_notional"),
        "cash": sizing.get("cash"),
        "min_cash_balance": sizing.get("min_cash_balance"),
    }


def watcher_activity(project_root, watchers_path, report_date):
    start_at, end_at = day_window(report_date)
    buys = []
    cash_blocked = []
    latest_status = {}

    for watcher in configured_watchers(project_root, watchers_path):
        for record in iter_watcher_log_records(watcher["log_path"], start_at, end_at):
            result = record.get("result") or {}
            symbol = record.get("symbol") or result.get("symbol") or watcher.get("symbol")
            status = result.get("status")
            latest_status[symbol] = {
                "timestamp": record.get("timestamp"),
                "status": status,
                "position_qty": result.get("position_qty"),
                "current_price": result.get("current_price"),
            }
            if status in BUY_STATUSES:
                buys.append(
                    {
                        "timestamp": record.get("timestamp"),
                        "symbol": symbol,
                        "status": status,
                        "reason": result.get("reason"),
                        "qty": result.get("qty"),
                        "limit_price": result.get("limit_price"),
                        "order_id": result.get("reentry_order_id"),
                    }
                )
            if status in BLOCKED_STATUSES:
                cash_blocked.append(cash_blocked_detail(record, result))
            for skipped in result.get("skipped_ladder_orders", []):
                if skipped.get("status") == "cash_reserve_blocked":
                    detail = cash_blocked_detail(record, {"status": "ladder_cash_reserve_blocked", "sizing": skipped.get("sizing", {})})
                    detail["symbol"] = symbol
                    detail["drop_step_percent"] = skipped.get("drop_step_percent")
                    cash_blocked.append(detail)

    return {
        "bot_buy_orders_submitted": buys,
        "cash_blocked_buy_signals": cash_blocked,
        "latest_watcher_status": latest_status,
    }


def fill_side(fill):
    side = (fill.get("side") or fill.get("order_side") or "").lower()
    if side:
        return side
    qty = float(fill.get("qty") or fill.get("net_qty") or 0)
    return "buy" if qty > 0 else "sell" if qty < 0 else ""


def summarize_fills(fills):
    bought = []
    sold = []
    for fill in fills:
        item = {
            "timestamp": fill.get("transaction_time") or fill.get("date"),
            "symbol": fill.get("symbol"),
            "qty": fill.get("qty"),
            "price": fill.get("price"),
            "order_id": fill.get("order_id"),
        }
        side = fill_side(fill)
        if side == "buy":
            bought.append(item)
        elif side == "sell":
            sold.append(item)
    return bought, sold


def summarize_positions(positions):
    summaries = []
    for position in positions or []:
        qty = as_float(position.get("qty"))
        current_price = as_float(position.get("current_price"))
        avg_entry_price = as_float(position.get("avg_entry_price"))
        market_value = as_float(position.get("market_value"))
        total_pl = as_float(position.get("unrealized_pl"))
        total_plpc = as_float(position.get("unrealized_plpc"))
        day_pl = as_float(position.get("unrealized_intraday_pl"))
        day_plpc = as_float(position.get("unrealized_intraday_plpc"))
        summaries.append(
            {
                "symbol": position.get("symbol"),
                "qty": qty,
                "avg_entry_price": avg_entry_price,
                "current_price": current_price,
                "market_value": market_value,
                "total_gain_loss": total_pl,
                "total_gain_loss_percent": total_plpc * 100,
                "daily_gain_loss": day_pl,
                "daily_gain_loss_percent": day_plpc * 100,
            }
        )
    return sorted(summaries, key=lambda item: item.get("symbol") or "")


def pct_change(start_value, end_value):
    if start_value in (None, 0) or end_value is None:
        return None
    return round((float(end_value) / float(start_value) - 1) * 100, 4)


def portfolio_summary(client, account, reporting_config=None):
    reporting_config = reporting_config or {}
    history = client.portfolio_history(period="1A", timeframe="1D")
    values = [float(value) for value in history.get("equity", []) if value is not None]
    day_gain_percent = None
    total_gain_percent = None
    total_gain_baseline = reporting_config.get("total_gain_baseline")
    portfolio_value = float(account.get("portfolio_value", values[-1] if values else 0))
    if len(values) >= 2:
        day_gain_percent = pct_change(values[-2], values[-1])
    if values:
        total_gain_percent = pct_change(values[0], portfolio_value)
    if total_gain_percent is None and total_gain_baseline not in (None, "", 0):
        total_gain_percent = pct_change(float(total_gain_baseline), portfolio_value)
    return {
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
        "equity": account.get("equity"),
        "portfolio_value": account.get("portfolio_value"),
        "day_gain_percent": day_gain_percent,
        "total_gain_percent": total_gain_percent,
        "total_gain_baseline": total_gain_baseline,
    }


def build_report(project_root, watchers_path, report_date, client=None):
    start_at, end_at = day_window(report_date)
    supervisor_config = load_json(watchers_path, {})
    reporting_config = supervisor_config.get("reporting", {})
    account_summary = {}
    bought = []
    sold = []
    positions = []
    account_error = None

    if client:
        try:
            account = client.account()
            account_summary = portfolio_summary(client, account, reporting_config)
            fills = client.fills(iso_utc(start_at), iso_utc(end_at))
            bought, sold = summarize_fills(fills)
            positions = summarize_positions(client.positions())
        except Exception as exc:
            account_error = f"{type(exc).__name__}: {exc}"

    activity = watcher_activity(project_root, watchers_path, report_date)
    return {
        "report_date": report_date.isoformat(),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": account_summary,
        "account_error": account_error,
        "current_positions": positions,
        "stocks_bought": bought,
        "stocks_sold": sold,
        **activity,
    }


def money(value):
    if value in (None, ""):
        return "n/a"
    return f"${float(value):,.2f}"


def percent(value):
    if value is None:
        return "n/a"
    return f"{float(value):.2f}%"


def number(value):
    if value in (None, ""):
        return "n/a"
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:,.4f}".rstrip("0").rstrip(".")


def short_time(value):
    timestamp = parse_time(value)
    if not timestamp:
        return "n/a"
    return timestamp.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def aggregate_fills(fills):
    grouped = {}
    for fill in fills or []:
        key = (fill.get("symbol"), fill.get("order_id"))
        item = grouped.setdefault(
            key,
            {
                "symbol": fill.get("symbol"),
                "order_id": fill.get("order_id"),
                "qty": 0.0,
                "notional": 0.0,
                "first_time": fill.get("timestamp"),
                "last_time": fill.get("timestamp"),
                "fills": 0,
            },
        )
        qty = as_float(fill.get("qty"))
        price = as_float(fill.get("price"))
        item["qty"] += qty
        item["notional"] += qty * price
        item["fills"] += 1
        item["last_time"] = fill.get("timestamp") or item["last_time"]
    summaries = []
    for item in grouped.values():
        avg_price = item["notional"] / item["qty"] if item["qty"] else None
        summaries.append({**item, "avg_price": avg_price})
    return sorted(summaries, key=lambda item: (item.get("symbol") or "", item.get("last_time") or ""))


def aggregate_cash_blocks(blocks):
    grouped = defaultdict(
        lambda: {
            "symbol": None,
            "count": 0,
            "first_time": None,
            "last_time": None,
            "max_requested_notional": 0.0,
            "max_requested_qty": 0.0,
            "best_available_notional": 0.0,
            "modes": set(),
        }
    )
    for block in blocks or []:
        symbol = block.get("symbol") or "UNKNOWN"
        item = grouped[symbol]
        item["symbol"] = symbol
        item["count"] += 1
        item["first_time"] = item["first_time"] or block.get("timestamp")
        item["last_time"] = block.get("timestamp") or item["last_time"]
        item["max_requested_notional"] = max(
            item["max_requested_notional"],
            as_float(block.get("target_notional")),
        )
        item["max_requested_qty"] = max(
            item["max_requested_qty"],
            as_float(block.get("requested_qty")),
        )
        item["best_available_notional"] = max(
            item["best_available_notional"],
            as_float(block.get("available_notional")),
        )
        if block.get("mode"):
            item["modes"].add(block["mode"])

    summaries = []
    for item in grouped.values():
        summaries.append(
            {
                **item,
                "modes": ", ".join(sorted(item["modes"])) or "n/a",
            }
        )
    return sorted(summaries, key=lambda item: (-item["count"], item["symbol"]))


def markdown_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return lines


def render_fill_table(items, empty_text):
    summaries = aggregate_fills(items)
    if not summaries:
        return [empty_text]
    rows = []
    for item in summaries:
        rows.append(
            [
                item.get("symbol") or "n/a",
                number(item.get("qty")),
                money(item.get("avg_price")),
                money(item.get("notional")),
                item.get("fills"),
                short_time(item.get("last_time")),
            ]
        )
    return markdown_table(["Symbol", "Qty", "Avg Price", "Notional", "Fills", "Last Fill"], rows)


def render_bot_order_table(items):
    if not items:
        return ["No bot buy submissions found in watcher logs."]
    rows = []
    for item in items:
        rows.append(
            [
                item.get("symbol") or "n/a",
                number(item.get("qty")),
                money(item.get("limit_price")),
                item.get("reason") or "n/a",
                short_time(item.get("timestamp")),
            ]
        )
    return markdown_table(["Symbol", "Qty", "Limit", "Reason", "Submitted"], rows)


def render_cash_block_table(items):
    summaries = aggregate_cash_blocks(items)
    if not summaries:
        return ["No cash-blocked buy signals found."]
    rows = []
    for item in summaries:
        rows.append(
            [
                item["symbol"],
                item["count"],
                money(item["max_requested_notional"]),
                number(item["max_requested_qty"]),
                money(item["best_available_notional"]),
                short_time(item["first_time"]),
                short_time(item["last_time"]),
            ]
        )
    return markdown_table(
        [
            "Symbol",
            "Signals",
            "Largest Target",
            "Max Qty",
            "Best Cash Above Reserve",
            "First",
            "Last",
        ],
        rows,
    )


def signed_money(value):
    if value in (None, ""):
        return "n/a"
    numeric = float(value)
    sign = "+" if numeric > 0 else ""
    return f"{sign}${numeric:,.2f}"


def signed_percent(value):
    if value is None:
        return "n/a"
    numeric = float(value)
    sign = "+" if numeric > 0 else ""
    return f"{sign}{numeric:.2f}%"


def render_positions_table(positions):
    if not positions:
        return ["No open Alpaca positions found."]
    rows = []
    for item in positions:
        rows.append(
            [
                item.get("symbol") or "n/a",
                number(item.get("qty")),
                money(item.get("market_value")),
                money(item.get("avg_entry_price")),
                money(item.get("current_price")),
                f"{signed_money(item.get('total_gain_loss'))} ({signed_percent(item.get('total_gain_loss_percent'))})",
                f"{signed_money(item.get('daily_gain_loss'))} ({signed_percent(item.get('daily_gain_loss_percent'))})",
            ]
        )
    return markdown_table(
        [
            "Symbol",
            "Qty",
            "Market Value",
            "Avg Entry",
            "Current",
            "Total P/L",
            "Day P/L",
        ],
        rows,
    )


def render_markdown(report):
    account = report.get("account") or {}
    positions = report.get("current_positions") or []
    bought = report.get("stocks_bought") or []
    sold = report.get("stocks_sold") or []
    bot_buys = report.get("bot_buy_orders_submitted") or []
    cash_blocked = report.get("cash_blocked_buy_signals") or []
    lines = [
        f"# TraderBot Daily Report - {report['report_date']}",
        "",
        f"Generated: {short_time(report.get('generated_at'))}",
        "",
        "## Snapshot",
    ]
    lines.extend(
        markdown_table(
            ["Portfolio", "Day Gain", "Total Gain", "Cash", "Buying Power"],
            [
                [
                    money(account.get("portfolio_value")),
                    percent(account.get("day_gain_percent")),
                    percent(account.get("total_gain_percent")),
                    money(account.get("cash")),
                    money(account.get("buying_power")),
                ]
            ],
        )
    )
    if report.get("account_error"):
        lines.extend(["", f"Account data error: {report['account_error']}"])

    lines.extend(
        [
            "",
            "## Activity Summary",
            f"- Current open positions: {len(positions)}.",
            f"- Alpaca buy fills: {len(bought)} fill(s) across {len(aggregate_fills(bought))} order(s).",
            f"- Alpaca sell fills: {len(sold)} fill(s) across {len(aggregate_fills(sold))} order(s).",
            f"- Bot buy submissions: {len(bot_buys)} order(s).",
            f"- Cash-blocked buy signals: {len(cash_blocked)} signal(s) across {len(aggregate_cash_blocks(cash_blocked))} symbol(s).",
            "",
            "## Current Positions",
        ]
    )
    lines.extend(render_positions_table(positions))
    lines.extend(
        [
            "",
            "## Bought",
        ]
    )
    lines.extend(render_fill_table(bought, "No Alpaca buy fills found."))
    lines.extend(["", "## Sold"])
    lines.extend(render_fill_table(sold, "No Alpaca sell fills found."))
    lines.extend(["", "## Bot Buy Orders Submitted"])
    lines.extend(render_bot_order_table(bot_buys))
    lines.extend(["", "## Buy Signals Blocked By Cash"])
    lines.extend(render_cash_block_table(cash_blocked))
    lines.extend(
        [
            "",
            "Detailed event records are preserved in the matching JSON report.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_items(items, empty_text):
    if not items:
        return [f"- {empty_text}"]
    lines = []
    for item in items:
        clean = {key: value for key, value in item.items() if value not in (None, "", [])}
        details = ", ".join(f"{key}={value}" for key, value in clean.items())
        lines.append(f"- {details}")
    return lines


def write_report(report, report_dir):
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily_report_{report['report_date']}"
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    save_json(json_path, report)
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_WATCHERS_PATH)
    parser.add_argument("--date", help="Report date in YYYY-MM-DD. Defaults to last closed trading day.")
    parser.add_argument("--output-dir", default=DEFAULT_REPORT_DIR)
    parser.add_argument("--offline", action="store_true", help="Skip Alpaca account and fill lookups.")
    args = parser.parse_args()

    project_root = Path(args.config).resolve().parent.parent
    report_date = (
        datetime.date.fromisoformat(args.date)
        if args.date
        else last_closed_trading_day()
    )
    client = None
    if not args.offline:
        load_env(project_root / ".env")
        client = AlpacaClient()

    report = build_report(
        project_root,
        Path(args.config).resolve(),
        report_date,
        client=client,
    )
    _, markdown_path = write_report(report, project_root / args.output_dir)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
