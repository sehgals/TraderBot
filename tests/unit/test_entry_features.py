import copy
import datetime
import unittest

from traderbot.core_strategy_engine.entry_models.features import build_entry_features
from traderbot.core_strategy_engine.entry_models.breakout import evaluate_breakout
from traderbot.core_strategy_engine.entry_models.pullback import evaluate_pullback


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
    def test_trend_uses_actual_ema_change_and_unfloored_atr(self):
        bars = indicator_bars()
        bars[-1]['atr14'] = 0.005
        features = build_entry_features('TEST', bars, regime_bars(bars, True))
        self.assertAlmostEqual(features['ema21_change_5bars'], 0.075)
        self.assertEqual(features['trend_atr14'], 0.005)
        candidate = evaluate_breakout(features)
        slope = candidate['trend_assessment']['components'][-1]
        self.assertAlmostEqual(slope['observed'], 15)
        self.assertEqual(slope['unit'], 'atr')
        self.assertTrue(slope['passed'])

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

    def test_breakout_evaluator_has_only_breakout_specific_checks(self):
        bars = indicator_bars()
        for index, bar in enumerate(bars):
            bar.update(
                {
                    "h": 102.0 if index == 54 else 101.0,
                    "l": 99.0,
                    "c": 102.0 if index == 54 else 100.0,
                    "ema9": 101.0 if index == 54 else 99.5,
                    "ema21": 100.0 if index == 54 else 98.0 + index * 0.02,
                    "ema50": 98.0,
                    "atr14": 1.0,
                    "vwap": 99.0,
                    "volume_ratio": 2.0 if index == 54 else 1.0,
                }
            )
        market = regime_bars(bars, True)
        features = build_entry_features(
            "TEST", bars, market, sector_bars=market
        )

        candidate = evaluate_breakout(features)

        self.assertEqual(candidate["status"], "active_signal")
        self.assertEqual(candidate["model_id"], "breakout_continuation")
        self.assertEqual(candidate["setup_score"], 95)
        self.assertNotIn("touched_pullback", candidate["checks"])
        self.assertNotIn("no_chase", candidate["checks"])

    def test_pullback_evaluator_has_only_pullback_specific_checks(self):
        bars = indicator_bars()
        for index, bar in enumerate(bars):
            bar.update(
                {
                    "h": 106.0 if 34 <= index < 54 else 101.0,
                    "l": 99.0,
                    "c": 100.5 if index == 54 else 100.0,
                    "ema9": 100.1 if index == 54 else 99.5,
                    "ema21": 100.0 if index == 54 else 98.0 + index * 0.02,
                    "ema50": 98.0,
                    "atr14": 1.0,
                    "vwap": 99.0,
                    "volume_ratio": 1.25 if index == 54 else 1.0,
                }
            )
        market = regime_bars(bars, True)
        features = build_entry_features(
            "TEST", bars, market, sector_bars=market
        )

        candidate = evaluate_pullback(
            features,
            {"entry_models": {"pullback": {"minimum_reward_risk": 1.0}}},
        )

        self.assertEqual(candidate["status"], "active_signal")
        self.assertEqual(candidate["model_id"], "pullback_reclaim")
        self.assertEqual(candidate["setup_score"], 100)
        self.assertNotIn("breakout_now", candidate["checks"])
        self.assertIn("touched_pullback", candidate["checks"])

    def test_model_configuration_does_not_leak_between_evaluators(self):
        bars = indicator_bars()
        for index, bar in enumerate(bars):
            bar.update(
                {
                    "h": 102.0 if index == 54 else 101.0,
                    "l": 99.0,
                    "c": 102.0 if index == 54 else 100.0,
                    "ema9": 101.0 if index == 54 else 99.5,
                    "ema21": 100.0 if index == 54 else 98.0 + index * 0.02,
                    "ema50": 98.0,
                    "atr14": 1.0,
                    "vwap": 99.0,
                    "volume_ratio": 2.0 if index == 54 else 1.0,
                }
            )
        market = regime_bars(bars, True)
        features = build_entry_features(
            "TEST", bars, market, sector_bars=market
        )
        config = {"entry_models": {"pullback": {"minimum_rvol": 3.0}}}

        pullback = evaluate_pullback(features, config)
        breakout = evaluate_breakout(features, config)

        self.assertFalse(pullback["checks"]["volume_ok"])
        self.assertTrue(breakout["checks"]["volume_ok"])


if __name__ == "__main__":
    unittest.main()
