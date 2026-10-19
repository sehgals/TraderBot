"""Unit tests for Phase 1: Universe Archetypes and Convention-over-Configuration."""

import json
from pathlib import Path
import tempfile
import unittest

from traderbot.universe.archetypes import (
    DEFAULT_ARCHETYPES,
    load_archetypes,
    resolve_watcher_paths,
    resolve_watcher_strategy_defaults,
)
from traderbot.monitoring.supervisor import load_supervisor_config


class UniverseArchetypesTests(unittest.TestCase):
    def test_default_archetypes_loaded(self):
        archetypes = load_archetypes()
        self.assertIn("default", archetypes)
        self.assertIn("large_cap_tech", archetypes)
        self.assertIn("high_beta_growth", archetypes)
        self.assertIn("defensive_value", archetypes)
        self.assertEqual(archetypes["default"]["risk_profile"], "large_cap")
        self.assertEqual(archetypes["high_beta_growth"]["risk_profile"], "high_beta")
        self.assertEqual(archetypes["high_beta_growth"]["dynamic_entry_notional"], 3000)

    def test_convention_paths_managed(self):
        root = Path("C:/TraderBot")
        config_path, state_path, log_path = resolve_watcher_paths(
            symbol="NVDA",
            group="managed",
            project_root=root,
        )
        self.assertIsNone(config_path)
        self.assertEqual(state_path, root / "runtime" / "state" / "nvda_strategy_state.json")
        self.assertEqual(log_path, root / "runtime" / "logs" / "nvda_watcher.jsonl")

    def test_convention_paths_candidates(self):
        root = Path("C:/TraderBot")
        config_path, state_path, log_path = resolve_watcher_paths(
            symbol="TSLA",
            group="new",
            project_root=root,
        )
        self.assertIsNone(config_path)
        self.assertEqual(state_path, root / "runtime" / "state" / "candidates" / "tsla_strategy_state.json")
        self.assertEqual(log_path, root / "runtime" / "logs" / "candidates" / "tsla_watcher.jsonl")

    def test_explicit_paths_preserved(self):
        root = Path("C:/TraderBot")
        config_path, state_path, log_path = resolve_watcher_paths(
            symbol="NVDA",
            group="managed",
            project_root=root,
            config_file="custom_config.json",
            state_file="custom_state.json",
            log_file="custom_log.jsonl",
        )
        self.assertEqual(config_path, root / "custom_config.json")
        self.assertEqual(state_path, root / "custom_state.json")
        self.assertEqual(log_path, root / "custom_log.jsonl")

    def test_resolve_strategy_defaults_with_archetype(self):
        watcher = {
            "symbol": "RKLB",
            "archetype": "high_beta_growth",
            "config_defaults": {"dynamic_entry_notional": 4000},
        }
        supervisor_config = {
            "managed_watcher_defaults": {"minimum_entry_setup_score": 80},
        }
        defaults = resolve_watcher_strategy_defaults(
            symbol="RKLB",
            watcher=watcher,
            supervisor_config=supervisor_config,
        )
        self.assertEqual(defaults["symbol"], "RKLB")
        self.assertEqual(defaults["risk_profile"], "high_beta")
        # Overridden by watcher config_defaults
        self.assertEqual(defaults["dynamic_entry_notional"], 4000)
        # Inherited from group defaults
        self.assertEqual(defaults["minimum_entry_setup_score"], 80)
        # Inherited from archetype
        self.assertEqual(defaults["trail_stop_below_current_percent"], 4.0)

    def test_load_supervisor_config_with_minimal_watcher_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "watchers.json"
            minimal_config = {
                "new_watchers": [
                    {"symbol": "NVDA", "archetype": "large_cap_tech"},
                    {"symbol": "PLTR"},  # Uses default archetype
                ],
                "managed_watcher_defaults": {},
                "new_watcher_defaults": {},
            }
            config_path.write_text(json.dumps(minimal_config), encoding="utf-8")

            root, cfg, prepared = load_supervisor_config(config_path)
            self.assertEqual(len(prepared), 2)

            nvda = prepared[0]
            self.assertEqual(nvda["symbol"], "NVDA")
            self.assertEqual(nvda["group"], "new")
            self.assertIsNone(nvda["config_path"])
            self.assertEqual(nvda["state_path"], root / "runtime" / "state" / "candidates" / "nvda_strategy_state.json")
            self.assertEqual(nvda["log_path"], root / "runtime" / "logs" / "candidates" / "nvda_watcher.jsonl")
            self.assertEqual(nvda["config_defaults"]["risk_profile"], "large_cap")
            self.assertEqual(nvda["config_defaults"]["strategy_type"], "new_candidate_dynamic_entry")

            pltr = prepared[1]
            self.assertEqual(pltr["symbol"], "PLTR")
            self.assertEqual(pltr["state_path"], root / "runtime" / "state" / "candidates" / "pltr_strategy_state.json")
            self.assertEqual(pltr["config_defaults"]["dynamic_entry_notional"], 5000)


if __name__ == "__main__":
    unittest.main()
