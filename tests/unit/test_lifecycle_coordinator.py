import unittest

from traderbot.core_strategy_engine.lifecycle.coordinator import select_action_intent
from traderbot.core_strategy_engine.engine import position_action_intents


class LifecycleCoordinatorTests(unittest.TestCase):
    def test_exit_wins_over_reduce_add_and_entry(self):
        selected = select_action_intent(
            [
                {"action_id": "entry", "action": "enter"},
                {"action_id": "add", "action": "add"},
                {"action_id": "reduce", "action": "reduce"},
                {"action_id": "exit", "action": "exit"},
            ]
        )

        self.assertEqual(selected["action_id"], "exit")

    def test_restore_protection_has_highest_priority(self):
        selected = select_action_intent(
            [
                {"action_id": "exit", "action": "exit"},
                {"action_id": "protect", "action": "restore_protection"},
            ]
        )

        self.assertEqual(selected["action_id"], "protect")

    def test_explicit_portfolio_priority_can_override_default(self):
        selected = select_action_intent(
            [
                {"action_id": "health-exit", "action": "exit"},
                {
                    "action_id": "portfolio-emergency",
                    "action": "portfolio_reduce",
                    "priority": 110,
                },
            ]
        )

        self.assertEqual(selected["action_id"], "portfolio-emergency")

    def test_no_intents_returns_none(self):
        self.assertIsNone(select_action_intent([]))

    def test_confirmed_health_exit_beats_hard_reduction(self):
        config = {
            "symbol": "WAT",
            "position_health": {
                "shadow_mode": False,
                "exit_confirmation_bars": 2,
            },
        }
        state = {
            "position_episode": {"episode_id": "episode-1"},
            "position_health_confirmation": {"action": "exit", "count": 2},
        }
        health = {
            "recommended_action": "exit",
            "data_fresh": True,
        }

        selected = select_action_intent(
            position_action_intents(
                config,
                state,
                {"symbol": "WAT", "qty": "10", "avg_entry_price": "100"},
                90,
                health,
            )
        )

        self.assertEqual(selected["action"], "exit")
        self.assertEqual(selected["source"], "position_health")

    def test_hard_reduction_beats_health_add(self):
        config = {
            "symbol": "WAT",
            "position_health": {
                "shadow_mode": False,
                "additions_enabled": True,
            },
        }
        health = {
            "recommended_action": "hold",
            "data_fresh": True,
        }

        selected = select_action_intent(
            position_action_intents(
                config,
                {"position_episode": {"episode_id": "episode-1"}},
                {"symbol": "WAT", "qty": "10", "avg_entry_price": "100"},
                94,
                health,
            )
        )

        self.assertEqual(selected["source"], "hard_adverse_reduction")


if __name__ == "__main__":
    unittest.main()
