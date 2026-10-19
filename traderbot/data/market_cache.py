"""Shared in-memory market data cache with time-to-live (TTL).

Prevents rate-limit exhaustion by deduplicating high-frequency market data queries
(such as SPY and Sector ETF bars) across concurrent watchers in a scheduler batch.
"""

import threading
import time
from typing import Any, Dict, List, Optional, Tuple


class SharedMarketDataCache:
    """Thread-safe TTL cache for historical stock bars and quotes."""

    def __init__(self, ttl_seconds: float = 45.0):
        self.ttl = max(0.01, float(ttl_seconds))
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str, str, str], Tuple[List[Dict[str, Any]], float]] = {}
        self._hits = 0
        self._misses = 0

    def _make_key(self, symbol: str, timeframe: str, start: str, end: str) -> Tuple[str, str, str, str]:
        return (str(symbol or "").upper().strip(), str(timeframe), str(start), str(end))

    def get_or_fetch_bars(
        self,
        client: Any,
        symbol: str,
        start: str,
        end: str,
        timeframe: str,
    ) -> List[Dict[str, Any]]:
        """Retrieve bars from cache if fresh, otherwise fetch from client and cache."""
        key = self._make_key(symbol, timeframe, start, end)
        now = time.monotonic()

        with self._lock:
            if key in self._cache:
                bars, cached_at = self._cache[key]
                if (now - cached_at) <= self.ttl:
                    self._hits += 1
                    return [dict(b) for b in bars]

        # Fetch outside lock to prevent blocking concurrent readers of other symbols
        bars = client.stock_bars(symbol, start, end, timeframe) if hasattr(client, "stock_bars") else []

        with self._lock:
            self._cache[key] = ([dict(b) for b in (bars or [])], now)
            self._misses += 1

        return [dict(b) for b in (bars or [])]

    def invalidate(self, symbol: Optional[str] = None) -> None:
        """Invalidate cache entries for a given symbol or all symbols."""
        with self._lock:
            if symbol is None:
                self._cache.clear()
            else:
                sym = str(symbol).upper().strip()
                keys_to_remove = [k for k in self._cache if k[0] == sym]
                for k in keys_to_remove:
                    self._cache.pop(k, None)

    def clear(self) -> None:
        """Clear all cached market data."""
        self.invalidate(None)

    def stats(self) -> Dict[str, Any]:
        """Return hit/miss statistics and cache size."""
        with self._lock:
            total = self._hits + self._misses
            ratio = round(self._hits / total, 4) if total > 0 else 0.0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "total_requests": total,
                "hit_ratio": ratio,
                "cached_series_count": len(self._cache),
                "ttl_seconds": self.ttl,
            }
