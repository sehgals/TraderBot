import datetime
import json

from traderbot.monitoring.reports import (
    enrich_bot_orders_with_broker_status,
    enrich_sold_fills_from_watchers,
    portfolio_summary,
    render_bot_order_table,
    render_cash_block_table,
    render_fill_table,
    render_markdown,
    render_position_health_table,
    render_positions_table,
    signal_strength,
)


class FakeClient:
    def portfolio_history(self, **_kwargs):
        return {"equity": [50000, 53000]}


class FakeOrderClient:
    def order(self, _order_id):
        return {
            "status": "expired",
            "time_in_force": "day",
            "extended_hours": False,
            "submitted_at": "2026-08-12T13:30:13Z",
            "expired_at": "2026-08-12T20:00:49Z",
        }


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


def test_positions_table_pads_columns_for_plain_text_alignment():
    positions = [
        {"symbol": "A", "qty": 1, "market_value": 10},
        {"symbol": "LONG", "qty": 1000, "market_value": 12345},
    ]

    table = render_positions_table(positions)
    pipe_positions = [[index for index, char in enumerate(line) if char == "|"] for line in table]

    assert all(positions == pipe_positions[0] for positions in pipe_positions[1:])
    assert "| Symbol " in table[0]
    assert "|      1 " in table[2]


def test_bought_and_sold_tables_pad_columns_for_plain_text_alignment():
    bought = render_fill_table(
        [
            {
                "symbol": "A",
                "qty": 1,
                "price": 10,
                "order_id": "buy-1",
                "timestamp": "2026-07-28T14:00:00Z",
            }
        ],
        "No buys.",
    )
    sold = render_fill_table(
        [
            {
                "symbol": "LONG",
                "qty": 1000,
                "price": 12,
                "avg_entry_price": 10,
                "realized_pl": 2000,
                "order_id": "sell-1",
                "timestamp": "2026-07-28T15:00:00Z",
            }
        ],
        "No sells.",
        include_realized_pl=True,
    )

    for table in (bought, sold):
        pipe_positions = [
            [index for index, char in enumerate(line) if char == "|"]
            for line in table
        ]
        assert all(row == pipe_positions[0] for row in pipe_positions[1:])

    assert "|      1 " in bought[2]
    assert "|   1000 " in sold[2]


def test_snapshot_table_pads_columns_for_plain_text_alignment():
    markdown = render_markdown(
        {
            "report_date": "2026-08-05",
            "account": {
                "portfolio_value": 123456.78,
                "day_gain_percent": 1.25,
                "total_gain_percent": 12.5,
                "cash": 987.65,
                "margin_used": 0,
                "buying_power": 5432.10,
            },
            "current_positions": [],
        }
    )
    lines = markdown.splitlines()
    snapshot_start = lines.index("## Snapshot") + 1
    table = lines[snapshot_start : snapshot_start + 3]
    pipe_positions = [[index for index, char in enumerate(line) if char == "|"] for line in table]

    assert all(row == pipe_positions[0] for row in pipe_positions[1:])


def test_bot_order_and_cash_block_tables_pad_columns_for_plain_text_alignment():
    bot_orders = render_bot_order_table(
        [
            {
                "symbol": "A",
                "qty": 1,
                "limit_price": 10,
                "reason": "breakout",
                "timestamp": "2026-08-04T14:00:00Z",
            },
            {
                "symbol": "LONG",
                "qty": 1000,
                "limit_price": 123.45,
                "reason": "dynamic_breakout_continuation",
                "timestamp": "2026-08-04T15:00:00Z",
            },
        ]
    )
    cash_blocks = render_cash_block_table(
        [
            {
                "symbol": "A",
                "timestamp": "2026-08-04T14:00:00Z",
                "cash": 100,
                "min_cash_balance": 50,
                "target_notional": 500,
                "requested_qty": 5,
                "available_notional": 50,
            },
            {
                "symbol": "LONG",
                "timestamp": "2026-08-04T15:00:00Z",
                "cash": 1000,
                "min_cash_balance": 250,
                "target_notional": 2000,
                "requested_qty": 20,
                "available_notional": 750,
            },
        ]
    )

    for table in (bot_orders, cash_blocks):
        pipe_positions = [[index for index, char in enumerate(line) if char == "|"] for line in table]
        assert all(row == pipe_positions[0] for row in pipe_positions[1:])


def test_bot_order_table_includes_broker_lifecycle():
    orders = enrich_bot_orders_with_broker_status(
        FakeOrderClient(),
        [{"symbol": "CRDO", "order_id": "order-1", "qty": 10, "limit_price": 229.8}],
    )

    rendered = "\n".join(render_bot_order_table(orders))

    assert "TIF" in rendered
    assert "Final Status" in rendered
    assert "DAY" in rendered
    assert "Expired" in rendered
    assert "6h 30m 36s" in rendered


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


def test_report_explains_position_health_method():
    markdown = render_markdown({"report_date": "2026-07-10", "current_positions": []})

    assert "### Position Health Method" in markdown
    assert "entry-relative downside (45%)" in markdown
    assert "Entry Setup scores are reserved for flat candidates" in markdown
    assert "risk-management assessment, not a return prediction" in markdown


def test_positions_table_uses_position_health_not_entry_signal():
    table = render_positions_table(
        [
            {
                "symbol": "WAT",
                "qty": 4,
                "market_value": 1640,
                "avg_entry_price": 400,
                "current_price": 410,
                "total_gain_loss": 40,
                "total_gain_loss_percent": 2.5,
                "position_health_state": "Healthy",
                "position_health_score": 88,
                "position_health_action": "hold",
                "position_health_remaining_r": 1.75,
                "signal_strength": "Weak",
                "signal_score": 40,
            }
        ]
    )
    rendered = "\n".join(table)

    assert "Position Health" in rendered
    assert "Healthy (88%)" in rendered
    assert "1.75" in rendered
    assert "Signal Strength" not in rendered
    assert "Weak (40%)" not in rendered


def test_position_health_section_shows_components_protection_and_reasons():
    positions = [
        {
            "symbol": "WAT",
            "position_health_state": "At Risk",
            "position_health_score": 38,
            "position_health_action": "reduce",
            "position_health_entry_return_percent": -5.8553,
            "position_health_downside_score": 32,
            "position_health_trend_score": 10,
            "position_health_reward_risk_score": 100,
            "position_health_remaining_r": 3.3824,
            "position_health_stop_price": 16.03,
            "position_health_stop_qty": 71,
            "position_health_data_fresh": True,
            "position_health_data_complete": True,
            "position_health_as_of": "2026-08-12T18:00:00Z",
            "position_health_reasons": ["structural_trend_failure"],
        }
    ]

    rendered = "\n".join(render_position_health_table(positions))

    assert "At Risk (38%)" in rendered
    assert "-5.86%" in rendered
    assert "$16.03 x 71" in rendered
    assert "Fresh / Complete" in rendered
    assert "structural_trend_failure" in rendered

    markdown = render_markdown({"report_date": "2026-08-12", "current_positions": positions})
    assert "## Position Health" in markdown


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
