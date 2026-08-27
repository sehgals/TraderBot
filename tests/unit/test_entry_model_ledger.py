import datetime
import unittest

from traderbot.core_strategy_engine.entry_models.breakout import evaluate_breakout
from traderbot.core_strategy_engine.entry_models.pullback import evaluate_pullback


def features(ledger_cap=90):
    bar = {
        "c": 102,
        "ema9": 101,
        "ema21": 100,
        "ema50": 99,
        "vwap": 100,
    }
    return {
        "symbol": "TEST",
        "as_of": datetime.datetime(2026, 8, 27, 14, 0, tzinfo=datetime.timezone.utc),
        "bar": bar,
        "previous_bar": {"c": 100, "l": 99},
        "recent_three_bars": [bar, bar, bar],
        "atr14": 1,
        "recent_high_20": 101,
        "recent_low_20": 98,
        "ema21_slope_5bars": 0.01,
        "relative_dollar_volume": 2,
        "ledger_cap": ledger_cap,
        "market_ok": True,
        "sector_ok": True,
        "above_exit": True,
        "no_same_day_loss_reentry": True,
        "entry_filters": {"checks": {}, "metrics": {}},
    }


class EntryModelLedgerTests(unittest.TestCase):
    def test_breakout_above_ledger_cap_is_blocked_without_repricing(self):
        candidate = evaluate_breakout(features())

        self.assertEqual(candidate["status"], "watch")
        self.assertIn("ledger_price_ok", candidate["blockers"])
        self.assertAlmostEqual(candidate["limit_price"], 101.1)
        self.assertGreater(candidate["stop_price"], 90)

    def test_pullback_above_ledger_cap_is_blocked_without_repricing(self):
        candidate = evaluate_pullback(features())

        self.assertEqual(candidate["status"], "watch")
        self.assertIn("ledger_price_ok", candidate["blockers"])
        self.assertAlmostEqual(candidate["limit_price"], 100.1)
        self.assertGreater(candidate["stop_price"], 90)


if __name__ == "__main__":
    unittest.main()
