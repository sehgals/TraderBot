import datetime as dt
import json
import time

from traderbot.data.tastytrade_stream import TastytradeLiveStream


class WebSocketTimeoutException(Exception):
    pass


class FakeSocket:
    def __init__(self, messages, fail_when_empty=False):
        self.messages = [json.dumps(message) for message in messages]
        self.fail_when_empty = fail_when_empty
        self.sent = []
        self.closed = False

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.fail_when_empty:
            raise ConnectionError("connection dropped")
        time.sleep(0.005)
        raise WebSocketTimeoutException()

    def settimeout(self, _seconds):
        pass

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, ws):
        self.sockets = ws if isinstance(ws, list) else [ws]
        self.open_count = 0

    def open_stream(self, symbols, start, timeframe):
        self.open_count += 1
        return self.sockets[self.open_count - 1], dt.datetime.fromisoformat(
            start.replace("Z", "+00:00")
        ), "5m"

    def _send(self, ws, message):
        ws.sent.append(message)

    def latest_quotes(self, _symbols):
        return {}


def test_persistent_stream_keeps_alive_and_preserves_quiet_symbols():
    now = dt.datetime.now(dt.timezone.utc)
    milliseconds = int(now.timestamp() * 1000)
    ws = FakeSocket([{"type": "FEED_DATA", "data": [
        "Quote", ["Quote", "MU", milliseconds, 100.0, 100.1, 10, 12],
        "Candle", ["Candle", "MU{=5m}", milliseconds, 99, 101, 98, 100, 1000],
    ]}])
    client = FakeClient(ws)
    start = (now - dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    stream = TastytradeLiveStream(client, ["MU", "WDC"], start,
                                  keepalive_seconds=0.01).start_background()
    try:
        assert stream.wait_ready(1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            data, events = stream.snapshot(["MU", "WDC"], dt.datetime.now(dt.timezone.utc))
            if events >= 2 and ws.sent:
                break
            time.sleep(0.01)
        assert client.open_count == 1
        assert data["MU"]["quote"]["bid_price"] == 100.0
        assert len(data["MU"]["bars"]) == 1
        assert data["WDC"]["bars"] == []
        assert {message["type"] for message in ws.sent} == {"KEEPALIVE"}
    finally:
        stream.stop()
    assert ws.closed


def test_persistent_stream_reconnects_and_deduplicates_candles():
    now = dt.datetime.now(dt.timezone.utc)
    first_time = int((now - dt.timedelta(minutes=2)).timestamp() * 1000)
    second_time = int((now - dt.timedelta(minutes=1)).timestamp() * 1000)
    candle = lambda stamp: {"type": "FEED_DATA", "data": [
        "Candle", ["Candle", "MU{=5m}", stamp, 99, 101, 98, 100, 1000],
    ]}
    first = FakeSocket([candle(first_time)], fail_when_empty=True)
    second = FakeSocket([candle(first_time), candle(second_time)])
    client = FakeClient([first, second])
    start = (now - dt.timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    stream = TastytradeLiveStream(client, ["MU"], start).start_background()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.open_count >= 2:
                try:
                    data, _ = stream.snapshot(["MU"], dt.datetime.now(dt.timezone.utc))
                except RuntimeError:
                    pass
                else:
                    if len(data["MU"]["bars"]) == 2:
                        break
            time.sleep(0.01)
        assert client.open_count == 2
        assert len(data["MU"]["bars"]) == 2
        assert first.closed
    finally:
        stream.stop()
    assert second.closed
