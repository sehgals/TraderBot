import datetime
import unittest

from traderbot.core_strategy_engine.entry_models.filters import evaluate_entry_filters


UTC = datetime.timezone.utc


class EntryFilterTests(unittest.TestCase):
    def features(self, when=None):
        return {
            "symbol": "IBM",
            "as_of": when or datetime.datetime(2026, 8, 20, 15, tzinfo=UTC),
            "bar": {
                "matched_average_dollar_volume": 750_000,
                "dollar_volume_sample_size": 20,
                "session_gap_percent": 2.0,
            },
        }

    def test_bar_and_quote_filters_pass(self):
        result = evaluate_entry_filters(
            self.features(),
            {
                "risk_profile": "large_cap",
                "entry_filters": {
                    "liquidity": {"enabled": True},
                    "spread": {"enabled": True, "maximum_percent": 0.25},
                    "gap": {"enabled": True, "maximum_absolute_percent": 5},
                },
            },
            {
                "evaluated_at": self.features()["as_of"],
                "quote": {
                    "bid_price": 100.0,
                    "ask_price": 100.2,
                    "timestamp": "2026-08-20T14:59:45Z",
                },
            },
        )
        self.assertTrue(all(result["checks"].values()))
        self.assertAlmostEqual(result["metrics"]["quoted_spread_percent"], 0.1998, places=3)

    def test_missing_quote_fails_closed(self):
        result = evaluate_entry_filters(
            self.features(),
            {"entry_filters": {"spread": {"enabled": True}}},
        )
        self.assertEqual(result["blockers"], ["spread_data_available", "spread_ok"])

    def test_known_earnings_event_blocks_inside_window(self):
        result = evaluate_entry_filters(
            self.features(),
            {"entry_filters": {"earnings": {"enabled": True}}},
            {
                "event_calendar": {
                    "coverage": {
                        "point_in_time": True,
                        "symbols": ["IBM"],
                        "start": "2026-01-01",
                        "end": "2026-12-31",
                    },
                    "events": [
                        {
                            "symbol": "IBM",
                            "type": "earnings",
                            "date": "2026-08-21",
                            "known_at": "2026-08-01T12:00:00Z",
                        }
                    ],
                }
            },
        )
        self.assertTrue(result["checks"]["earnings_data_available"])
        self.assertFalse(result["checks"]["earnings_ok"])

    def test_calendar_without_point_in_time_contract_fails_closed(self):
        result = evaluate_entry_filters(
            self.features(),
            {"entry_filters": {"corporate_actions": {"enabled": True}}},
            {"event_calendar": {"coverage": {"symbols": ["IBM"]}, "events": []}},
        )
        self.assertFalse(result["checks"]["corporate_actions_data_available"])

    def test_calendar_requires_explicit_symbol_coverage(self):
        result = evaluate_entry_filters(
            self.features(),
            {"entry_filters": {"earnings": {"enabled": True}}},
            {
                "event_calendar": {
                    "coverage": {
                        "point_in_time": True,
                        "start": "2026-01-01",
                        "end": "2026-12-31",
                    },
                    "events": [],
                }
            },
        )

        self.assertFalse(result["checks"]["earnings_data_available"])


if __name__ == "__main__":
    unittest.main()
