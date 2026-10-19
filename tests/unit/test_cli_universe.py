"""Unit tests for Phase 6: Universe Management CLI."""

import argparse
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from traderbot.cli.universe import cmd_add, cmd_list, cmd_remove, cmd_validate


class UniverseCLITests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.config_path = self.root / "config" / "watchers.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_data = {
            "managed_watchers": [{"symbol": "MRVL", "config": "configs/mrvl.json"}],
            "new_watchers": [{"symbol": "TSEM"}],
        }
        self.config_path.write_text(json.dumps(self.initial_data), encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_cmd_list_prints_table(self):
        args = argparse.Namespace(config=str(self.config_path))
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            code = cmd_list(args)
            self.assertEqual(code, 0)
            output = mock_stdout.getvalue()
            self.assertIn("MRVL", output)
            self.assertIn("TSEM", output)
            self.assertIn("XLK", output)

    def test_cmd_validate(self):
        args = argparse.Namespace(symbol="NVDA", config=str(self.config_path))
        with patch("traderbot.cli.universe.get_alpaca_client", return_value=None):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                code = cmd_validate(args)
                self.assertEqual(code, 0)
                output = mock_stdout.getvalue()
                self.assertIn("XLK", output)
                self.assertIn("Information Technology", output)

    def test_cmd_add_and_remove(self):
        args_add = argparse.Namespace(
            symbol="AMD",
            archetype="large_cap_tech",
            group="candidate",
            dry_run=False,
            skip_validation=True,
            config=str(self.config_path),
        )
        code = cmd_add(args_add)
        self.assertEqual(code, 0)

        # Verify added to new_watchers
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        symbols = [w["symbol"] for w in data["new_watchers"]]
        self.assertIn("AMD", symbols)
        amd_entry = next(w for w in data["new_watchers"] if w["symbol"] == "AMD")
        self.assertEqual(amd_entry["archetype"], "large_cap_tech")

        # Duplicate add returns 0 with warning
        code_dup = cmd_add(args_add)
        self.assertEqual(code_dup, 0)

        # Remove AMD
        args_rem = argparse.Namespace(symbol="AMD", config=str(self.config_path))
        code_rem = cmd_remove(args_rem)
        self.assertEqual(code_rem, 0)

        data_after = json.loads(self.config_path.read_text(encoding="utf-8"))
        symbols_after = [w["symbol"] for w in data_after["new_watchers"]]
        self.assertNotIn("AMD", symbols_after)


if __name__ == "__main__":
    unittest.main()
