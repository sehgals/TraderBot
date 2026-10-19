"""Unit tests for Phase 5: SRE State Decoupling & Runtime Promotions."""

import json
from pathlib import Path
import tempfile
import unittest

from traderbot.monitoring.supervisor import (
    load_supervisor_config,
    promote_new_watcher_if_bought,
)


class SREPromotionsTests(unittest.TestCase):
    def test_promotion_records_to_runtime_registry_and_overlays(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            watchers_file = root / "config" / "watchers.json"
            watchers_file.parent.mkdir(parents=True, exist_ok=True)
            (root / "runtime" / "state" / "candidates").mkdir(parents=True, exist_ok=True)
            (root / "runtime" / "logs" / "candidates").mkdir(parents=True, exist_ok=True)

            initial_config = {
                "managed_watchers": [],
                "new_watchers": [
                    {"symbol": "TSLA", "archetype": "large_cap_tech"}
                ],
                "managed_watcher_defaults": {},
                "new_watcher_defaults": {},
            }
            watchers_file.write_text(json.dumps(initial_config), encoding="utf-8")

            # Load initial config
            _, supervisor_cfg, prepared = load_supervisor_config(watchers_file)
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0]["group"], "new")

            # Simulate buying TSLA
            watcher = prepared[0]
            # Create dummy state and log files
            watcher["state_path"].write_text("{}", encoding="utf-8")
            watcher["log_path"].write_text("", encoding="utf-8")

            result = {"status": "filled", "order_id": "order-1", "position_qty": 5}
            strategy_cfg = {"dynamic_entry_enabled": True, "reentry_enabled": False}

            promoted = promote_new_watcher_if_bought(
                project_root=root,
                watchers_config_path=watchers_file,
                supervisor_config=supervisor_cfg,
                watcher=watcher,
                strategy_config=strategy_cfg,
                result=result,
            )
            self.assertIsNotNone(promoted)
            self.assertEqual(promoted["symbol"], "TSLA")

            # Verify saved to runtime promotions file
            promotions_file = root / "runtime" / "state" / "promoted_watchers.json"
            self.assertTrue(promotions_file.exists())
            promotions_data = json.loads(promotions_file.read_text(encoding="utf-8"))
            self.assertEqual(len(promotions_data["promoted_watchers"]), 1)
            self.assertEqual(promotions_data["promoted_watchers"][0]["symbol"], "TSLA")

            # Re-loading supervisor config merges promotions automatically
            _, reloaded_cfg, reloaded_prepared = load_supervisor_config(watchers_file)
            self.assertEqual(len(reloaded_prepared), 1)
            self.assertEqual(reloaded_prepared[0]["symbol"], "TSLA")
            self.assertEqual(reloaded_prepared[0]["group"], "managed")


if __name__ == "__main__":
    unittest.main()
