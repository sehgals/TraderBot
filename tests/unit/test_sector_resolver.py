"""Unit tests for Phase 2: Dynamic Sector Resolver."""

import unittest

from traderbot.universe.sector_resolver import (
    etf_to_sector_name,
    resolve_sector_etf,
    sector_name_to_etf,
)


class SectorResolverTests(unittest.TestCase):
    def test_explicit_override_in_position_health(self):
        config = {
            "position_health": {
                "benchmark_by_symbol": {
                    "PLUG": "ICLN",
                    "CUSTOM": "XLU",
                }
            }
        }
        self.assertEqual(resolve_sector_etf("PLUG", config), "ICLN")
        self.assertEqual(resolve_sector_etf("CUSTOM", config), "XLU")

    def test_known_symbols_auto_resolved(self):
        # Technology
        self.assertEqual(resolve_sector_etf("NVDA"), "XLK")
        self.assertEqual(resolve_sector_etf("AAPL"), "XLK")
        self.assertEqual(resolve_sector_etf("MSFT"), "XLK")
        self.assertEqual(resolve_sector_etf("MRVL"), "XLK")

        # Industrials
        self.assertEqual(resolve_sector_etf("GE"), "XLI")
        self.assertEqual(resolve_sector_etf("CAT"), "XLI")
        self.assertEqual(resolve_sector_etf("GD"), "XLI")
        self.assertEqual(resolve_sector_etf("RKLB"), "XLI")

        # Healthcare
        self.assertEqual(resolve_sector_etf("LLY"), "XLV")
        self.assertEqual(resolve_sector_etf("UNH"), "XLV")
        self.assertEqual(resolve_sector_etf("AZN"), "XLV")
        self.assertEqual(resolve_sector_etf("WAT"), "XLV")

        # Communication
        self.assertEqual(resolve_sector_etf("GOOG"), "XLC")
        self.assertEqual(resolve_sector_etf("META"), "XLC")
        self.assertEqual(resolve_sector_etf("NFLX"), "XLC")

        # Financials
        self.assertEqual(resolve_sector_etf("JPM"), "XLF")
        self.assertEqual(resolve_sector_etf("LMND"), "XLF")

        # Energy
        self.assertEqual(resolve_sector_etf("XOM"), "XLE")
        self.assertEqual(resolve_sector_etf("PUMP"), "XLE")

    def test_fallback_to_configured_benchmark_or_spy(self):
        # Unknown ticker with configured benchmark
        config = {
            "position_health": {
                "benchmark_symbol": "QQQ",
            }
        }
        self.assertEqual(resolve_sector_etf("UNKNOWN_TICKER", config), "QQQ")

        # Unknown ticker with no config -> falls back to SPY (never 'Unmapped')
        self.assertEqual(resolve_sector_etf("SOME_OBSCURE_STOCK"), "SPY")

    def test_empty_or_whitespace_symbol(self):
        self.assertEqual(resolve_sector_etf(""), "SPY")
        self.assertEqual(resolve_sector_etf(None), "SPY")
        self.assertEqual(resolve_sector_etf("  "), "SPY")

    def test_sector_name_to_etf(self):
        self.assertEqual(sector_name_to_etf("Technology"), "XLK")
        self.assertEqual(sector_name_to_etf("Information Technology"), "XLK")
        self.assertEqual(sector_name_to_etf("Industrials"), "XLI")
        self.assertEqual(sector_name_to_etf("Health Care"), "XLV")
        self.assertEqual(sector_name_to_etf("Clean Tech"), "ICLN")
        self.assertEqual(sector_name_to_etf("Unknown Sector"), "SPY")

    def test_etf_to_sector_name(self):
        self.assertEqual(etf_to_sector_name("XLK"), "Information Technology")
        self.assertEqual(etf_to_sector_name("XLI"), "Industrials")
        self.assertEqual(etf_to_sector_name("XLV"), "Health Care")
        self.assertEqual(etf_to_sector_name("ICLN"), "Clean Energy")


if __name__ == "__main__":
    unittest.main()
