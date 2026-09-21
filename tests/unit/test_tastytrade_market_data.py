import json
import socket

import pytest

from traderbot.data.feed_comparison import compare_source_observations
from traderbot.data.tastytrade_market_data import TastytradeMarketDataClient


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = [json.dumps(message) for message in messages]
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        raise socket.timeout()

    def settimeout(self, _value):
        pass

    def close(self):
        self.closed = True


def make_client(**kwargs):
    return TastytradeMarketDataClient(
        client_id="client", client_secret="secret", refresh_token="refresh", **kwargs
    )


def test_oauth_token_is_cached_until_near_expiration():
    client = make_client()
    requests = []

    def request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return {"access_token": "access", "expires_in": 900}

    client._request = request

    assert client.access_token() == "access"
    assert client.access_token() == "access"
    assert len(requests) == 1
    assert requests[0][1] == "/oauth/token"
    assert requests[0][2]["authenticated"] is False


def test_rest_quote_is_normalized():
    client = make_client()
    client._request = lambda *_args, **_kwargs: {
        "data": {"items": [{
            "symbol": "MU", "bid": "124.10", "ask": "124.14",
            "bid-size": "12", "ask-size": "9", "last": "124.12",
            "volume": "1250000", "updated-at": "2026-08-20T15:00:00Z",
        }]}
    }

    quote = client.latest_quote("mu")

    assert quote["feed"] == "tastytrade_rest"
    assert quote["bid_price"] == 124.10
    assert quote["ask_price"] == 124.14
    assert quote["volume"] == 1_250_000


def test_dxlink_handshake_subscription_and_event_normalization():
    event_time = 1787238000000
    ws = FakeWebSocket([
        {"type": "SETUP", "channel": 0},
        {"type": "AUTH_STATE", "channel": 0, "state": "UNAUTHORIZED"},
        {"type": "AUTH_STATE", "channel": 0, "state": "AUTHORIZED"},
        {"type": "CHANNEL_OPENED", "channel": 3},
        {"type": "FEED_CONFIG", "channel": 3},
        {"type": "FEED_DATA", "channel": 3, "data": [
            "Quote", ["Quote", "MU", event_time, 124.10, 124.14, 12, 9],
            "Trade", ["Trade", "MU", event_time, 124.12, 1_250_000, 100],
            "Candle", ["Candle", "MU{=5m}", event_time, 123, 125, 122, 124, 50_000],
        ]},
    ])
    client = make_client(websocket_factory=lambda _url, _timeout: ws)
    client.quote_token = lambda: {"token": "quote-token", "dxlink-url": "wss://example"}

    result = client.stream_snapshot(
        ["MU"], "2026-08-20T13:00:00Z", "5Min", wait_seconds=0.01
    )

    assert result["MU"]["quote"]["bid_price"] == 124.10
    assert result["MU"]["quote"]["last_price"] == 124.12
    assert result["MU"]["bars"][0]["c"] == 124.0
    assert ws.closed is True
    subscription = next(item for item in ws.sent if item["type"] == "FEED_SUBSCRIPTION")
    assert {item["type"] for item in subscription["add"]} == {"Quote", "Trade", "Candle"}
    candle = next(item for item in subscription["add"] if item["type"] == "Candle")
    assert candle["symbol"] == "MU{=5m}"


def test_generic_comparison_labels_tastytrade_separately():
    alpaca = {"quote": {"feed": "iex", "bid_price": 99, "ask_price": 101}}
    tasty = {"quote": {"feed": "tastytrade_dxlink", "bid_price": 100, "ask_price": 102}}

    result = compare_source_observations(
        "MU", alpaca, tasty, "2026-08-20T15:00:00Z", "alpaca", "tastytrade"
    )

    assert result["primary_source"] == "alpaca"
    assert result["secondary_source"] == "tastytrade"
    assert result["tastytrade_midpoint"] == 101
    assert result["midpoint_difference_bps"] == pytest.approx(100)
