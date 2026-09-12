import datetime

import pytest

from traderbot.core_strategy_engine.engine import AlpacaClient


def test_latest_quote_normalizes_alpaca_fields():
    client = object.__new__(AlpacaClient)
    requested = []
    client.data = lambda method, path: (
        requested.append((method, path))
        or {"quote": {"bp": 100.0, "ap": 100.2, "t": "2026-08-20T15:00:00Z"}}
    )

    quote = client.latest_quote("IBM")

    assert {k:quote[k] for k in ("bid_price", "ask_price", "timestamp")} == {
        "bid_price": 100.0,
        "ask_price": 100.2,
        "timestamp": "2026-08-20T15:00:00Z",
    }
    assert quote["feed"] == "iex"
    assert requested == [("GET", "/stocks/IBM/quotes/latest?feed=iex")]


def test_fills_paginates_bare_list_activity_responses():
    client = object.__new__(AlpacaClient)
    first_page = [{"id": f"activity-{index}"} for index in range(100)]
    second_page = [{"id": "activity-100"}]
    requested_paths = []

    def trading(_method, path, _payload=None):
        requested_paths.append(path)
        return first_page if len(requested_paths) == 1 else second_page

    client.trading = trading

    fills = client.fills("2026-01-01T00:00:00Z", "2026-07-17T00:00:00Z")

    assert len(fills) == 101
    assert "page_token=activity-99" in requested_paths[1]


def test_buy_submission_rejects_stale_open_market_clock():
    client = object.__new__(AlpacaClient)
    stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
    client.clock = lambda: {"is_open": True, "timestamp": stale.isoformat()}
    client.open_buy_orders = lambda _symbol: []
    client.trading = lambda *_args, **_kwargs: pytest.fail("order POST must not be called")

    with pytest.raises(RuntimeError, match="market clock is stale"):
        client.submit_order({"symbol": "WAT", "side": "buy", "qty": "6"})


def test_buy_submission_rejects_existing_open_buy_order():
    client = object.__new__(AlpacaClient)
    now = datetime.datetime.now(datetime.timezone.utc)
    client.clock = lambda: {"is_open": True, "timestamp": now.isoformat()}
    client.open_buy_orders = lambda _symbol: [{"id": "overnight-buy"}]
    client.trading = lambda *_args, **_kwargs: pytest.fail("order POST must not be called")

    with pytest.raises(RuntimeError, match="overnight-buy"):
        client.submit_order({"symbol": "WAT", "side": "buy", "qty": "6"})


def test_buy_submission_with_fresh_clock_and_no_open_order_posts_order():
    client = object.__new__(AlpacaClient)
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {"symbol": "WAT", "side": "buy", "qty": "6"}
    client.clock = lambda: {"is_open": True, "timestamp": now.isoformat()}
    client.open_buy_orders = lambda _symbol: []
    client.trading = lambda method, path, submitted: {
        "id": "new-buy",
        "method": method,
        "path": path,
        **submitted,
    }

    order = client.submit_order(payload)

    assert order["id"] == "new-buy"
    assert order["method"] == "POST"
    assert order["path"] == "/orders"
