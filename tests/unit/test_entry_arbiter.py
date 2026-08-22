import unittest

from traderbot.core_strategy_engine.entry_models.arbiter import (
    select_entry_candidate,
)


def candidate(model_id, status="active_signal", last_price=100, trigger=None, score=90, rr=2):
    result = {
        "model_id": model_id,
        "status": status,
        "last_price": last_price,
        "setup_score": score,
        "expected_reward_risk": rr,
        "limit_price": 100,
        "risk_per_share": 2,
    }
    if trigger is not None:
        result["breakout_trigger"] = trigger
    return result


class EntryArbiterTests(unittest.TestCase):
    def test_breakout_territory_cannot_fall_back_to_pullback(self):
        pullback = candidate("pullback_reclaim")
        breakout = candidate(
            "breakout_continuation",
            status="watch",
            last_price=102,
            trigger=101,
        )

        decision = select_entry_candidate([pullback, breakout])

        self.assertIsNone(decision["selected"])
        self.assertIs(decision["classified"], breakout)
        self.assertEqual(decision["reason"], "breakout_territory")

    def test_pullback_territory_selects_active_pullback(self):
        pullback = candidate("pullback_reclaim")
        breakout = candidate(
            "breakout_continuation",
            status="watch",
            last_price=100,
            trigger=101,
        )

        decision = select_entry_candidate([breakout, pullback])

        self.assertIs(decision["selected"], pullback)
        self.assertEqual(decision["reason"], "pullback_territory")

    def test_generic_ranking_is_independent_of_input_order(self):
        lower = candidate("model_b", score=90, rr=1.5)
        higher = candidate("model_a", score=95, rr=1.5)

        first = select_entry_candidate([lower, higher])
        second = select_entry_candidate([higher, lower])

        self.assertEqual(first["selected"]["model_id"], "model_a")
        self.assertEqual(second["selected"]["model_id"], "model_a")


if __name__ == "__main__":
    unittest.main()
