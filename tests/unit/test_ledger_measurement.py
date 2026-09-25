from traderbot.backtester.reentry_backtest import AlpacaData, pair_completed_trades
from traderbot.core_strategy_engine.engine import (
    append_episode_exit_event,
    exit_attribution,
    record_exit_intent,
)


def test_research_fill_reader_paginates_bare_activity_lists():
    data = object.__new__(AlpacaData)
    first = [{"id": f"fill-{index}"} for index in range(100)]
    second = [{"id": "fill-100"}]
    paths = []

    def trading(path):
        paths.append(path)
        return first if len(paths) == 1 else second

    data.trading = trading

    assert len(data.fills("2026-01-01T00:00:00Z")) == 101
    assert "page_token=fill-99" in paths[1]


def test_partial_sales_share_one_position_episode_and_keep_reasons():
    fills = [
        {"transaction_time": "2026-01-02T14:30:00Z", "symbol": "ABC", "side": "buy", "qty": "10", "price": "100", "order_id": "buy-1"},
        {"transaction_time": "2026-01-03T14:30:00Z", "symbol": "ABC", "side": "sell", "qty": "4", "price": "110", "order_id": "sell-1", "exit_reason": "health_reduction"},
        {"transaction_time": "2026-01-04T14:30:00Z", "symbol": "ABC", "side": "sell", "qty": "6", "price": "108", "order_id": "sell-2", "exit_reason": "trailing_stop"},
    ]

    trades = pair_completed_trades(fills, {"ABC"})

    assert [trade["exit_reason"] for trade in trades] == ["health_reduction", "trailing_stop"]
    assert trades[0]["episode_id"] == trades[1]["episode_id"] == "ABC:buy-1"


def test_exit_intent_is_durable_and_episode_event_is_idempotent():
    state = {"position_episode": {"episode_id": "ABC:buy-1"}}
    order = {"id": "sell-1", "qty": "5", "client_order_id": "ignored"}

    record_exit_intent(state, order, "hard_adverse_reduction")
    attribution = exit_attribution(state, "sell-1", order)
    details = {"order_id": "sell-1", "filled_qty": "5", **attribution}
    append_episode_exit_event(state, details)
    append_episode_exit_event(state, details)

    assert attribution == {
        "episode_id": "ABC:buy-1",
        "exit_reason": "hard_adverse_reduction",
    }
    assert len(state["position_episode"]["exit_events"]) == 1
