import copy
import datetime
import unittest

from traderbot.core_strategy_engine.entry_models.features import build_entry_features


def indicator_bars():
    start = datetime.datetime(2026, 1, 5, 14, 30, tzinfo=datetime.timezone.utc)
    bars = []
    for index in range(55):
        bars.append(
            {
                "t": start + datetime.timedelta(minutes=5 * index),
                "o": 100.0,
                "h": 101.0 + index * 0.01,
                "l": 99.0 - index * 0.01,
                "c": 100.0 + index * 0.02,
                "v": 1_000.0,
                "ema9": 99.5 + index * 0.02,
                "ema21": 99.0 + index * 0.015,
                "ema50": 98.0,
                "atr14": 1.25,
                "vwap": 99.25,
                "volume_ratio": 2.0 if index == 54 else 1.0,
            }
        )
    return bars


def regime_bars(stock_bars, favorable):
    return [
        {
            "t": stock_bars[-6 + index]["t"],
            "c": 102.0 if favorable else 99.0,
            "ema21": 100.0 + index * 0.1,
            "vwap": 100.0,
        }
        for index in range(6)
    ]


class EntryFeatureTests(unittest.TestCase):
    def test_requires_enough_completed_indicator_bars(self):
        bars = indicator_bars()

        self.assertIsNone(build_entry_features("TEST", bars[:50], bars[:50]))

    def test_build_is_pure_and_deterministic(self):
        bars = indicator_bars()
        original = copy.deepcopy(bars)
        market = regime_bars(bars, True)

        first = build_entry_features("TEST", bars, market)
        second = build_entry_features("TEST", bars, market)

        self.assertEqual(first, second)
        self.assertEqual(bars, original)

    def test_snapshot_contains_common_facts_but_no_model_specific_features(self):
        bars = indicator_bars()
        market = regime_bars(bars, True)
        sector = regime_bars(bars, False)
        exit_trade = {
            "exit_price": 101.0,
            "realized_pl": 25.0,
            "exit_time": bars[-1]["t"] - datetime.timedelta(days=1),
        }

        features = build_entry_features(
            "TEST",
            bars,
            market,
            sector_bars=sector,
            exit_trade=exit_trade,
        )

        self.assertTrue(features["market_ok"])
        self.assertFalse(features["sector_ok"])
        self.assertFalse(features["regime_ok"])
        self.assertTrue(features["above_exit"])
        self.assertAlmostEqual(features["ledger_cap"], 101.0 * 1.025)
        self.assertEqual(features["recent_high_20"], max(bar["h"] for bar in bars[-21:-1]))
        self.assertEqual(features["recent_low_20"], min(bar["l"] for bar in bars[-21:-1]))
        for model_specific_name in (
            "pullback_zone",
            "pullback_reclaim_trigger",
            "breakout_trigger",
            "stop_price",
            "target_price",
        ):
            self.assertNotIn(model_specific_name, features)


if __name__ == "__main__":
    unittest.main()
