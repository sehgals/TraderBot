"""A session-long, read-only DXLink feed for comparison observations."""

import datetime as dt
import json
import socket
import threading
import time

from traderbot.data.tastytrade_market_data import (
    UTC, _decode_compact, _iso_millis, _number,
)


class TastytradeLiveStream:
    def __init__(self, client, symbols, start, timeframe="5Min", keepalive_seconds=30):
        self.client = client
        self.symbols = tuple(dict.fromkeys(str(symbol).upper() for symbol in symbols))
        self.start = start
        self.timeframe = timeframe
        self.keepalive_seconds = keepalive_seconds
        self._start_at = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._connected = False
        self._last_error = None
        self._event_count = 0
        self._quotes = {
            symbol: {"feed": "tastytrade_dxlink", "symbol": symbol, "conditions": []}
            for symbol in self.symbols
        }
        self._bars = {symbol: {} for symbol in self.symbols}

    def start_background(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="tastytrade-dxlink", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def wait_ready(self, timeout):
        return self._ready.wait(timeout)

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            ws = None
            try:
                ws, _, _ = self.client.open_stream(self.symbols, self.start, self.timeframe)
                ws.settimeout(1.0)
                with self._lock:
                    self._connected = True
                    self._last_error = None
                self._ready.set()
                backoff = 1
                last_keepalive = time.monotonic()
                while not self._stop.is_set():
                    if time.monotonic() - last_keepalive >= self.keepalive_seconds:
                        self.client._send(ws, {"type": "KEEPALIVE", "channel": 0})
                        last_keepalive = time.monotonic()
                    try:
                        message = json.loads(ws.recv())
                    except Exception as exc:
                        if isinstance(exc, (socket.timeout, TimeoutError)) \
                                or "Timeout" in type(exc).__name__:
                            continue
                        raise
                    if message.get("type") == "FEED_DATA":
                        for event in _decode_compact(message.get("data")):
                            self._record_event(event)
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._connected = False
                self._ready.clear()
                if ws is not None:
                    ws.close()
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    def _record_event(self, event):
        kind = event.get("eventType")
        symbol = str(event.get("eventSymbol") or "").split("{")[0].upper()
        if symbol not in self._quotes:
            return
        now = dt.datetime.now(UTC)
        received_at = now.isoformat().replace("+00:00", "Z")
        with self._lock:
            if kind == "Quote":
                quote = self._quotes[symbol]
                quote.update({
                    "bid_price": _number(event.get("bidPrice")),
                    "ask_price": _number(event.get("askPrice")),
                    "bid_size": _number(event.get("bidSize")),
                    "ask_size": _number(event.get("askSize")),
                    "received_at": received_at,
                })
                timestamp = _iso_millis(event.get("eventTime"))
                if timestamp:
                    quote["timestamp"] = timestamp
            elif kind == "Trade":
                quote = self._quotes[symbol]
                quote.update({
                    "last_price": _number(event.get("price")),
                    "last_size": _number(event.get("size")),
                    "volume": _number(event.get("dayVolume")),
                    "received_at": received_at,
                })
                timestamp = _iso_millis(event.get("time"))
                if timestamp:
                    quote["timestamp"] = timestamp
            elif kind == "Candle":
                timestamp = _iso_millis(event.get("time"))
                if not timestamp:
                    return
                when = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if not self._start_at <= when <= now:
                    return
                self._bars[symbol][timestamp] = {
                    "t": timestamp,
                    "o": _number(event.get("open")),
                    "h": _number(event.get("high")),
                    "l": _number(event.get("low")),
                    "c": _number(event.get("close")),
                    "v": _number(event.get("volume")),
                    "feed": "tastytrade_dxlink",
                }
            else:
                return
            self._event_count += 1

    def snapshot(self, symbols, observed_at):
        with self._lock:
            if not self._connected:
                raise RuntimeError(self._last_error or "DXLink stream is not connected")
            result = {
                symbol: {
                    "quote": dict(self._quotes[symbol]),
                    "bars": [dict(self._bars[symbol][stamp])
                             for stamp in sorted(self._bars[symbol])
                             if dt.datetime.fromisoformat(
                                 stamp.replace("Z", "+00:00")
                             ) <= observed_at],
                }
                for symbol in symbols
            }
            event_count = self._event_count
        missing = [symbol for symbol, item in result.items()
                   if item["quote"].get("bid_price") is None
                   or item["quote"].get("ask_price") is None]
        if missing:
            try:
                rest_quotes = self.client.latest_quotes(missing)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                rest_quotes = {}
            for symbol in missing:
                quote = rest_quotes.get(symbol)
                if quote and quote.get("bid_price") is not None and quote.get("ask_price") is not None:
                    result[symbol]["quote"] = {**quote, "feed": "tastytrade_rest_fallback"}
        return result, event_count
