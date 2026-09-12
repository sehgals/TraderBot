import threading
from contextlib import contextmanager


class ExecutionGateway:
    """Serialize broker mutations and deduplicate explicit action intents.

    Read operations are delegated to the wrapped broker client.  Callers that
    provide a deterministic ``client_order_id`` receive restart-safe
    deduplication through Alpaca's client-order lookup endpoint.
    """

    def __init__(self, client):
        self.client = client
        self._locks_guard = threading.Lock()
        self._symbol_locks = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _symbol_lock(self, symbol):
        key = str(symbol or "ACCOUNT").upper()
        with self._locks_guard:
            return self._symbol_locks.setdefault(key, threading.RLock())

    @contextmanager
    def symbol_transaction(self, symbol):
        with self._symbol_lock(symbol):
            yield self

    def submit_order(self, payload):
        with self._symbol_lock(payload.get("symbol")):
            client_order_id = payload.get("client_order_id")
            if client_order_id and hasattr(self.client, "order_by_client_order_id"):
                existing = self.client.order_by_client_order_id(client_order_id)
                if existing:
                    return existing
            if payload.get("side") == "sell" and payload.get("type") != "stop":
                symbol = payload.get("symbol")
                pending_sells = [
                    order
                    for order in (self.client.open_orders() or [])
                    if order.get("symbol") == symbol
                    and order.get("side") == "sell"
                    and order.get("type") != "stop"
                ]
                if pending_sells:
                    order_ids = ", ".join(str(order.get("id")) for order in pending_sells)
                    raise RuntimeError(
                        f"sell order blocked: existing non-stop sell order(s) for {symbol}: "
                        f"{order_ids}"
                    )
                position = self.client.position(symbol)
                held_qty = int(float((position or {}).get("qty") or 0))
                requested_qty = int(float(payload.get("qty") or 0))
                if requested_qty <= 0 or requested_qty > held_qty:
                    raise RuntimeError(
                        f"sell order blocked: requested {requested_qty} share(s) for {symbol}, "
                        f"broker position has {held_qty}"
                    )
            try:
                return self.client.submit_order(payload)
            except Exception:
                if client_order_id and hasattr(self.client, "order_by_client_order_id"):
                    existing = self.client.order_by_client_order_id(client_order_id)
                    if existing:
                        return existing
                raise

    def replace_order(self, order_id, payload):
        symbol = payload.get("symbol")
        if not symbol:
            symbol = (self.client.order(order_id) or {}).get("symbol")
        with self._symbol_lock(symbol):
            return self.client.replace_order(order_id, payload)

    def cancel_order(self, order_id, symbol=None):
        if not symbol:
            symbol = (self.client.order(order_id) or {}).get("symbol")
        with self._symbol_lock(symbol):
            return self.client.cancel_order(order_id)


__all__ = ["ExecutionGateway"]
