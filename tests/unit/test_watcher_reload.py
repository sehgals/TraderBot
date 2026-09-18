import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from traderbot.monitoring.supervisor import WatcherListReloader, load_supervisor_config


class WatcherReloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "watchers.json"
        self.source = {
            "new_watcher_defaults": {"strategy_type": "new_candidate_dynamic_entry"},
            "new_watchers": [self.entry("AAA")],
            "portfolio_allocator": {"promotion_stage": "shadow"},
        }
        self.write()
        self.reloader = WatcherListReloader(self.path)
        _, self.config, self.watchers = load_supervisor_config(self.path)
        self.watchers[0].update(next_run_at=12345, failures=2)

    def entry(self, symbol, **extra):
        return {"symbol": symbol, "state": f"{symbol}.json",
                "log": f"{symbol}.jsonl", **extra}

    def write(self):
        self.path.write_text(json.dumps(self.source), encoding="utf-8")

    def reload(self):
        with patch("builtins.print"):
            return self.reloader.reload(self.config, self.watchers)

    def test_addition_preserves_existing_schedule_and_execution_controls(self):
        self.source["new_watchers"].append(self.entry("MSFT"))
        self.source["portfolio_allocator"]["promotion_stage"] = "full"
        self.write()
        config, watchers = self.reload()
        self.assertIs(watchers[0], self.watchers[0])
        self.assertEqual(watchers[0]["next_run_at"], 12345)
        self.assertEqual(watchers[0]["failures"], 2)
        self.assertEqual(watchers[1]["symbol"], "MSFT")
        self.assertEqual(watchers[1]["next_run_at"], 0)
        self.assertEqual(config["portfolio_allocator"]["promotion_stage"], "shadow")

    def test_removed_and_disabled_watchers(self):
        self.source["new_watchers"] = [self.entry("MSFT", enabled=False)]
        self.write()
        _, watchers = self.reload()
        self.assertEqual([w["symbol"] for w in watchers], ["MSFT"])
        self.assertFalse(watchers[0]["enabled"])

    def test_invalid_missing_and_duplicate_edits_retain_list_then_recover(self):
        for text in ['{', '{}', json.dumps({**self.source, "new_watchers": [
            self.entry("AAA"), self.entry("AAA")
        ]})]:
            self.path.write_text(text, encoding="utf-8")
            config, watchers = self.reload()
            self.assertIs(config, self.config)
            self.assertIs(watchers, self.watchers)
        self.path.unlink()
        self.assertIs(self.reload()[1], self.watchers)
        self.source["new_watchers"].append(self.entry("MSFT"))
        self.write()
        self.assertEqual(len(self.reload()[1]), 2)

    def test_unchanged_file_is_not_reparsed(self):
        with patch("traderbot.monitoring.supervisor.load_supervisor_config") as loader:
            self.assertIs(self.reload()[1], self.watchers)
            loader.assert_not_called()

    def test_changed_defaults_reschedule_watcher(self):
        self.source["new_watcher_defaults"]["poll_seconds"] = 45
        self.write()
        _, watchers = self.reload()
        self.assertEqual(watchers[0]["config_defaults"]["poll_seconds"], 45)
        self.assertEqual(watchers[0]["next_run_at"], 0)


if __name__ == "__main__":
    unittest.main()
