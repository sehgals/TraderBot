import datetime
import json

from traderbot.monitoring.reports import (
    enrich_sold_fills_from_watchers,
    portfolio_summary,
    render_markdown,
    render_positions_table,
    signal_strength,
)


class FakeClient:
    def portfolio_history(self, **_kwargs):
        return {"equity": [50000, 53000]}


def test_positions_table_sorts_total_percent_descending_with_missing_values_last():
    positions = [
        {"symbol": "LOSS", "total_gain_loss_percent": -4.5},
        {"symbol": "MISSING", "total_gain_loss_percent": None},
        {"symbol": "WIN", "total_gain_loss_percent": 12.25},
        {"symbol": "FLAT", "total_gain_loss_percent": 0},
    ]

    table = render_positions_table(positions)
    symbols = [line.split("|")[1].strip() for line in table[2:]]

    assert symbols == ["WIN", "FLAT", "LOSS", "MISSING"]


def test_portfolio_summary_excludes_margin_from_snapshot_funds():
    account = {
        "cash": "-38583.81",
        "buying_power": "79355.69",
        "non_marginable_buying_power": "4940.37",
        "portfolio_value": "53054.44",
        "equity": "53054.44",
    }

    summary = portfolio_summary(FakeClient(), account)

    assert summary["cash"] == 0
    assert summary["margin_used"] == 38583.81
    assert summary["buying_power"] == "4940.37"
    assert summary["cash_balance"] == "-38583.81"
    assert summary["margin_buying_power"] == "79355.69"


def test_signal_strength_uses_strategy_blockers():
    strong = signal_strength({"blockers": ["no_chase"], "last_bar_time": "2026-07-10T20:00:00Z"})
    weak = signal_strength({"blockers": [f"check_{index}" for index in range(7)]})

    assert strong["signal_strength"] == "Strong"
    assert strong["signal_score"] == 92
    assert weak["signal_strength"] == "Weak"


def test_report_explains_signal_strength_method():
    markdown = render_markdown({"report_date": "2026-07-10", "current_positions": []})

    assert "### Signal Strength Method" in markdown
    assert "13 checks" in markdown
    assert "technical indicator, not a prediction or guarantee" in markdown


def test_sold_fill_uses_latest_watcher_entry_price(tmp_path):
    log_path = tmp_path / "runtime" / "logs" / "xyz_watcher.jsonl"
    state_path = tmp_path / "runtime" / "state" / "xyz_strategy_state.json"
    config_path = tmp_path / "config" / "watchers.json"
    log_path.parent.mkdir(parents=True)
    state_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "managed_watchers": [
                    {
                        "symbol": "XYZ",
                        "log": "runtime/logs/xyz_watcher.jsonl",
                        "state": "runtime/state/xyz_strategy_state.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text("{}", encoding="utf-8")
    log_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-16T13:30:00Z",
                "symbol": "XYZ",
                "result": {"entry_fill_price": 10.0, "position_qty": 5},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    sold = [
        {
            "timestamp": "2026-07-16T14:00:00Z",
            "symbol": "XYZ",
            "qty": "5",
            "price": "12",
            "order_id": "sell-1",
        }
    ]
    result = enrich_sold_fills_from_watchers(
        sold,
        tmp_path,
        config_path,
        datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc),
    )

    assert result[0]["avg_entry_price"] == 10.0
    assert result[0]["realized_pl"] == 10.0
    assert result[0]["pl_source"] == "watcher"
