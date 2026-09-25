import json
import tempfile
import unittest
from pathlib import Path

from traderbot.monitoring.supervisor import run_margin_reducer


class FakeClient:
    def __init__(self):
        self.canceled = []
        self.submitted = []

    def account(self):
        return {"cash": "-2500"}

    def open_orders(self):
        return [
            {"id": "buy-1", "symbol": "NEW", "side": "buy", "type": "limit"},
            {"id": "stop-1", "symbol": "LOSS", "side": "sell", "type": "stop"},
        ]

    def cancel_order(self, order_id):
        self.canceled.append(order_id)

    def positions(self):
        return [
            {"symbol": "WIN", "qty": "20", "current_price": "100", "unrealized_plpc": "0.10"},
            {"symbol": "LOSS", "qty": "20", "current_price": "100", "unrealized_plpc": "-0.20"},
        ]

    def submit_order(self, payload):
        self.submitted.append(payload)
        return {"id": "sell-1"}


class MarginReductionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "auto_margin_reduction": {
                "enabled": True,
                "max_daily_liquidation_dollars": 5000,
                "max_order_dollars": 1000,
                "target_margin_dollars": 0,
            }
        }

    def test_closed_market_cancels_buys_but_does_not_sell(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            result = run_margin_reducer(client, self.config, Path(directory), {"is_open": False})

        self.assertEqual(result["status"], "market_closed_margin_reduction_deferred")
        self.assertEqual(client.canceled, ["buy-1"])
        self.assertEqual(client.submitted, [])

    def test_sells_weakest_position_with_incremental_cap(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "runtime" / "state"
            state_dir.mkdir(parents=True)
            watcher_state = state_dir / "loss_strategy_state.json"
            watcher_state.write_text(
                json.dumps({"position_episode": {"episode_id": "LOSS:entry-1"}}),
                encoding="utf-8",
            )
            config = {
                **self.config,
                "managed_watchers": [
                    {
                        "symbol": "LOSS",
                        "state": "runtime/state/loss_strategy_state.json",
                    }
                ],
            }
            result = run_margin_reducer(client, config, root, {"is_open": True})
            saved_state = json.loads(watcher_state.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "margin_reduction_order_submitted")
        self.assertEqual(result["symbol"], "LOSS")
        self.assertEqual(result["qty"], 10)
        self.assertEqual(client.canceled, ["buy-1", "stop-1"])
        self.assertEqual(client.submitted[0]["side"], "sell")
        self.assertEqual(
            saved_state["exit_order_intents"]["sell-1"]["exit_reason"],
            "portfolio_margin_reduction",
        )
        self.assertEqual(
            saved_state["exit_order_intents"]["sell-1"]["episode_id"],
            "LOSS:entry-1",
        )


if __name__ == "__main__":
    unittest.main()
