import unittest

from traderbot.core_strategy_engine.lifecycle.coordinator import select_action_intent


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


if __name__ == "__main__":
    unittest.main()
