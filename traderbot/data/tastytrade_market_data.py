"""Read-only tastytrade OAuth and DXLink market-data adapter."""

import datetime as dt
import json
import math
import os
import socket
import time
import urllib.parse
import urllib.request


UTC = dt.timezone.utc
TIMEFRAME_MAP = {
    "1Min": "1m", "5Min": "5m", "15Min": "15m",
    "30Min": "30m", "1Hour": "1h", "1Day": "1d",
}
EVENT_FIELDS = {
    "Quote": ["eventType", "eventSymbol", "eventTime", "bidPrice", "askPrice",
              "bidSize", "askSize"],
    "Trade": ["eventType", "eventSymbol", "time", "price", "dayVolume", "size"],
    "Candle": ["eventType", "eventSymbol", "time", "open", "high", "low", "close",
               "volume"],
}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _iso_millis(value):
    try:
        milliseconds = float(value)
        if not math.isfinite(milliseconds) or milliseconds <= 0:
            return None
        return dt.datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat().replace(
            "+00:00", "Z"
        )
    except (TypeError, ValueError, OSError):
        return None


def _decode_compact(data):
    """Expand DXLink COMPACT FEED_DATA into dictionaries."""
    decoded = []
    event_type = None
    for item in data or []:
        if isinstance(item, str):
            event_type = item
            continue
        if not isinstance(item, list) or not event_type or event_type not in EVENT_FIELDS:
            continue
        fields = EVENT_FIELDS[event_type]
        decoded.append(dict(zip(fields, item)))
    return decoded


class TastytradeMarketDataClient:
    """Read-only market-data client; it intentionally exposes no order methods."""

    market_data_feed = "tastytrade_dxlink"

    def __init__(self, client_id=None, client_secret=None, refresh_token=None,
                 base_url=None, user_agent=None, timeout=15, websocket_factory=None):
        self.client_id = client_id or os.environ.get("TASTYTRADE_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("TASTYTRADE_CLIENT_SECRET")
        self.refresh_token = refresh_token or os.environ.get("TASTYTRADE_REFRESH_TOKEN")
        self.base_url = (base_url or os.environ.get(
            "TASTYTRADE_API_URL", "https://api.tastyworks.com"
        )).rstrip("/")
        self.user_agent = user_agent or os.environ.get(
            "TASTYTRADE_USER_AGENT", "TraderBot-market-data/1.0"
        )
        self.timeout = timeout
        self.websocket_factory = websocket_factory
        self._access_token = None
        self._access_expires_at = 0.0
        missing = [name for name, value in {
            "TASTYTRADE_CLIENT_ID": self.client_id,
            "TASTYTRADE_CLIENT_SECRET": self.client_secret,
            "TASTYTRADE_REFRESH_TOKEN": self.refresh_token,
        }.items() if not value]
        if missing:
            raise ValueError(f"Missing tastytrade credentials: {', '.join(missing)}")

    def _request(self, method, path, params=None, payload=None, authenticated=True):
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.access_token()}"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            content = response.read().decode("utf-8")
            return json.loads(content) if content else None

    def access_token(self):
        if self._access_token and time.time() < self._access_expires_at:
            return self._access_token
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        result = self._request("POST", "/oauth/token", payload=payload, authenticated=False) or {}
        self._access_token = result.get("access_token")
        if not self._access_token:
            raise RuntimeError("tastytrade OAuth response did not include an access token")
        lifetime = max(60, int(result.get("expires_in", 900)))
        self._access_expires_at = time.time() + lifetime - 30
        return self._access_token

    def quote_token(self):
        result = self._request("GET", "/api-quote-tokens") or {}
        data = result.get("data") or {}
        if not data.get("token") or not data.get("dxlink-url"):
            raise RuntimeError("tastytrade quote-token response was incomplete")
        return data

    def latest_quotes(self, symbols):
        symbols = [str(symbol).upper() for symbol in symbols]
        if len(symbols) > 100:
            raise ValueError("tastytrade REST quote requests support at most 100 symbols")
        result = self._request(
            "GET", "/market-data/by-type", {"equity": ",".join(symbols)}
        ) or {}
        items = ((result.get("data") or {}).get("items") or [])
        return {str(item.get("symbol") or "").upper(): self._normalize_quote(item)
                for item in items}

    def latest_quote(self, symbol):
        symbol = symbol.upper()
        quote = self.latest_quotes([symbol]).get(symbol)
        if not quote:
            raise LookupError(f"tastytrade quote not found: {symbol}")
        return quote

    @staticmethod
    def _normalize_quote(item):
        return {
            "feed": "tastytrade_rest",
            "symbol": str(item.get("symbol") or "").upper(),
            "bid_price": _number(item.get("bid")),
            "ask_price": _number(item.get("ask")),
            "bid_size": _number(item.get("bid-size")),
            "ask_size": _number(item.get("ask-size")),
            "last_price": _number(item.get("last")),
            "volume": _number(item.get("volume")),
            "timestamp": item.get("updated-at"),
            "conditions": [],
        }

    def _open_websocket(self, url):
        if self.websocket_factory:
            return self.websocket_factory(url, self.timeout)
        try:
            import websocket
        except ImportError as exc:
            raise RuntimeError(
                "DXLink requires websocket-client; install requirements-market-data.txt"
            ) from exc
        return websocket.create_connection(url, timeout=self.timeout)

    @staticmethod
    def _send(ws, message):
        ws.send(json.dumps(message, separators=(",", ":")))

    def _receive_until(self, ws, predicate, deadline):
        while time.monotonic() < deadline:
            try:
                message = json.loads(ws.recv())
            except Exception as exc:
                if isinstance(exc, (socket.timeout, TimeoutError)) or "Timeout" in type(exc).__name__:
                    continue
                raise
            if predicate(message):
                return message
        raise TimeoutError("timed out waiting for DXLink handshake")

    def stream_snapshot(self, symbols, start, timeframe="5Min", wait_seconds=8):
        """Collect current quotes/trades and historical candles in one DXLink session."""
        symbols = [str(symbol).upper() for symbol in symbols]
        if len(symbols) > 100:
            raise ValueError("DXLink Candle subscriptions support at most 100 symbols")
        period = TIMEFRAME_MAP.get(timeframe)
        if not period:
            raise ValueError(f"Unsupported tastytrade timeframe: {timeframe}")
        start_at = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=UTC)
        quote_auth = self.quote_token()
        ws = self._open_websocket(quote_auth["dxlink-url"])
        deadline = time.monotonic() + self.timeout
        try:
            self._send(ws, {"type": "SETUP", "channel": 0,
                            "version": "0.1-DXF-JS/0.3.0", "keepaliveTimeout": 60,
                            "acceptKeepaliveTimeout": 60})
            self._receive_until(ws, lambda msg: msg.get("type") == "AUTH_STATE", deadline)
            self._send(ws, {"type": "AUTH", "channel": 0, "token": quote_auth["token"]})
            self._receive_until(
                ws, lambda msg: msg.get("type") == "AUTH_STATE"
                and msg.get("state") == "AUTHORIZED", deadline
            )
            self._send(ws, {"type": "CHANNEL_REQUEST", "channel": 3,
                            "service": "FEED", "parameters": {"contract": "AUTO"}})
            self._receive_until(ws, lambda msg: msg.get("type") == "CHANNEL_OPENED", deadline)
            self._send(ws, {"type": "FEED_SETUP", "channel": 3,
                            "acceptAggregationPeriod": 0.1,
                            "acceptDataFormat": "COMPACT",
                            "acceptEventFields": EVENT_FIELDS})
            self._receive_until(ws, lambda msg: msg.get("type") == "FEED_CONFIG", deadline)
            additions = []
            # DXLink time-series subscriptions use Unix epoch milliseconds.
            from_time = int(start_at.timestamp() * 1000)
            for symbol in symbols:
                additions.extend([
                    {"type": "Quote", "symbol": symbol},
                    {"type": "Trade", "symbol": symbol},
                    {"type": "Candle", "symbol": f"{symbol}{{={period}}}",
                     "fromTime": from_time},
                ])
            self._send(ws, {"type": "FEED_SUBSCRIPTION", "channel": 3,
                            "reset": True, "add": additions})
            if hasattr(ws, "settimeout"):
                ws.settimeout(min(1.0, max(0.1, wait_seconds)))
            snapshot = self._collect_events(
                ws, symbols, period, wait_seconds, start_at=start_at
            )
        finally:
            ws.close()

        missing_quotes = [
            symbol
            for symbol, item in snapshot.items()
            if item["quote"].get("bid_price") is None
            or item["quote"].get("ask_price") is None
        ]
        if missing_quotes:
            try:
                rest_quotes = self.latest_quotes(missing_quotes)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                rest_quotes = {}
            for symbol in missing_quotes:
                rest_quote = rest_quotes.get(symbol)
                if rest_quote and rest_quote.get("bid_price") is not None \
                        and rest_quote.get("ask_price") is not None:
                    rest_quote = dict(rest_quote)
                    rest_quote["feed"] = "tastytrade_rest_fallback"
                    snapshot[symbol]["quote"] = rest_quote
        return snapshot

    def _collect_events(self, ws, symbols, period, wait_seconds, start_at=None):
        quotes = {symbol: {"feed": "tastytrade_dxlink", "symbol": symbol,
                           "conditions": []} for symbol in symbols}
        bars = {symbol: [] for symbol in symbols}
        collected_from = start_at or dt.datetime.min.replace(tzinfo=UTC)
        if collected_from.tzinfo is None:
            collected_from = collected_from.replace(tzinfo=UTC)
        collected_from = collected_from.astimezone(UTC)
        deadline = time.monotonic() + wait_seconds
        last_keepalive = time.monotonic()
        while time.monotonic() < deadline:
            if time.monotonic() - last_keepalive >= 30:
                self._send(ws, {"type": "KEEPALIVE", "channel": 0})
                last_keepalive = time.monotonic()
            try:
                message = json.loads(ws.recv())
            except Exception as exc:
                if isinstance(exc, (socket.timeout, TimeoutError)) or "Timeout" in type(exc).__name__:
                    continue
                raise
            if message.get("type") != "FEED_DATA":
                continue
            for event in _decode_compact(message.get("data")):
                event_type = event.get("eventType")
                event_symbol = str(event.get("eventSymbol") or "")
                symbol = event_symbol.split("{")[0].upper()
                if symbol not in quotes:
                    continue
                if event_type == "Quote":
                    quotes[symbol].update({
                        "bid_price": _number(event.get("bidPrice")),
                        "ask_price": _number(event.get("askPrice")),
                        "bid_size": _number(event.get("bidSize")),
                        "ask_size": _number(event.get("askSize")),
                        "received_at": dt.datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    })
                    event_timestamp = _iso_millis(event.get("eventTime"))
                    if event_timestamp:
                        quotes[symbol]["timestamp"] = event_timestamp
                elif event_type == "Trade":
                    quotes[symbol].update({
                        "last_price": _number(event.get("price")),
                        "last_size": _number(event.get("size")),
                        "volume": _number(event.get("dayVolume")),
                        "received_at": dt.datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    })
                    trade_timestamp = _iso_millis(event.get("time"))
                    if trade_timestamp:
                        quotes[symbol]["timestamp"] = trade_timestamp
                elif event_type == "Candle":
                    bars[symbol].append({
                        "t": _iso_millis(event.get("time")),
                        "o": _number(event.get("open")), "h": _number(event.get("high")),
                        "l": _number(event.get("low")), "c": _number(event.get("close")),
                        "v": _number(event.get("volume")), "feed": "tastytrade_dxlink",
                    })
        for symbol in bars:
            collected_until = dt.datetime.now(UTC)
            by_timestamp = {}
            for bar in bars[symbol]:
                timestamp = bar.get("t")
                try:
                    parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except (AttributeError, TypeError, ValueError):
                    continue
                if collected_from <= parsed.astimezone(UTC) <= collected_until:
                    by_timestamp[timestamp] = bar
            bars[symbol] = [by_timestamp[key] for key in sorted(by_timestamp)]
        return {symbol: {"quote": quotes[symbol], "bars": bars[symbol],
                         "candle_period": period} for symbol in symbols}


__all__ = ["TastytradeMarketDataClient"]
