"""Unit tests for Phase 3: SharedMarketDataCache."""

import time
import unittest
from unittest.mock import MagicMock

from traderbot.data.market_cache import SharedMarketDataCache


class MarketCacheTests(unittest.TestCase):
    def test_cache_hit_and_miss(self):
        cache = SharedMarketDataCache(ttl_seconds=10.0)
        client = MagicMock()
        client.stock_bars.return_value = [{"t": "2026-10-19T14:00:00Z", "c": 500.0}]

        # First call: cache miss
        bars1 = cache.get_or_fetch_bars(client, "SPY", "2026-10-19T00:00:00Z", "2026-10-19T14:00:00Z", "5Min")
        self.assertEqual(len(bars1), 1)
        self.assertEqual(bars1[0]["c"], 500.0)
        self.assertEqual(client.stock_bars.call_count, 1)

        # Second call: cache hit (client.stock_bars not called again)
        bars2 = cache.get_or_fetch_bars(client, "SPY", "2026-10-19T00:00:00Z", "2026-10-19T14:00:00Z", "5Min")
        self.assertEqual(len(bars2), 1)
        self.assertEqual(bars2[0]["c"], 500.0)
        self.assertEqual(client.stock_bars.call_count, 1)

        stats = cache.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hit_ratio"], 0.5)

    def test_cache_ttl_expiration(self):
        cache = SharedMarketDataCache(ttl_seconds=0.1)
        client = MagicMock()
        client.stock_bars.return_value = [{"t": "2026-10-19T14:00:00Z", "c": 100.0}]

        # Call 1
        cache.get_or_fetch_bars(client, "AAPL", "start", "end", "1Hour")
        self.assertEqual(client.stock_bars.call_count, 1)

        # Wait for TTL to expire
        time.sleep(0.15)

        # Call 2 should trigger fresh fetch
        cache.get_or_fetch_bars(client, "AAPL", "start", "end", "1Hour")
        self.assertEqual(client.stock_bars.call_count, 2)

    def test_symbol_invalidation(self):
        cache = SharedMarketDataCache(ttl_seconds=60.0)
        client = MagicMock()
        client.stock_bars.return_value = [{"c": 200.0}]

        cache.get_or_fetch_bars(client, "NVDA", "s", "e", "5Min")
        cache.get_or_fetch_bars(client, "MSFT", "s", "e", "5Min")
        self.assertEqual(cache.stats()["cached_series_count"], 2)

        # Invalidate only NVDA
        cache.invalidate("NVDA")
        self.assertEqual(cache.stats()["cached_series_count"], 1)

        # Invalidate all
        cache.clear()
        self.assertEqual(cache.stats()["cached_series_count"], 0)


if __name__ == "__main__":
    unittest.main()
