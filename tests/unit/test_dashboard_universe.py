"""Unit tests for dashboard stock onboarding and universe endpoints."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traderbot.cli import dashboard  # Loads the isolated optional dependencies.
from fastapi.testclient import TestClient
from traderbot.dashboard.server import SnapshotStore, create_app
from traderbot.universe.onboarding import (
    add_stock_to_universe,
    get_available_archetypes,
    inspect_stock,
    remove_stock_from_universe,
    validate_symbol_format,
)


class DashboardUniverseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_dir = self.root / "config"
        self.config_dir.mkdir(parents=True)
        self.watchers_json = self.config_dir / "watchers.json"
        self.initial_watchers = {
            "managed_watchers": [{"symbol": "MRVL", "config": "configs/mrvl.json"}],
            "new_watchers": [{"symbol": "TSEM"}],
        }
        self.watchers_json.write_text(json.dumps(self.initial_watchers), encoding="utf-8")
        self.store = SnapshotStore(self.root)
        self.client = TestClient(create_app(self.store), base_url="http://127.0.0.1")

    def test_validate_symbol_format(self):
        self.assertEqual(validate_symbol_format("nvda"), "NVDA")
        self.assertEqual(validate_symbol_format("  aapl  "), "AAPL")
        self.assertEqual(validate_symbol_format("brk.b"), "BRK.B")
        with self.assertRaises(ValueError):
            validate_symbol_format("")
        with self.assertRaises(ValueError):
            validate_symbol_format("TOOLONGSYMBOLNAME")
        with self.assertRaises(ValueError):
            validate_symbol_format("INVALID$CHAR")

    def test_get_archetypes_endpoint(self):
        resp = self.client.get("/api/universe/archetypes")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        keys = [item["key"] for item in data]
        self.assertIn("default", keys)
        self.assertIn("large_cap_tech", keys)
        self.assertIn("high_beta_growth", keys)
        self.assertIn("defensive_value", keys)

    def test_inspect_endpoint(self):
        # Enrolled managed
        resp = self.client.get("/api/universe/inspect?symbol=MRVL")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["symbol"], "MRVL")
        self.assertTrue(data["is_enrolled"])
        self.assertEqual(data["enrolled_group"], "managed")
        self.assertFalse(data["ready"])

        # Enrolled candidate
        resp2 = self.client.get("/api/universe/inspect?symbol=TSEM")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["enrolled_group"], "candidate")

        # Unenrolled symbol
        resp3 = self.client.get("/api/universe/inspect?symbol=NVDA")
        self.assertEqual(resp3.status_code, 200)
        data3 = resp3.json()
        self.assertEqual(data3["symbol"], "NVDA")
        self.assertFalse(data3["is_enrolled"])
        self.assertEqual(data3["sector_etf"], "XLK")
        self.assertEqual(data3["sector_name"], "Information Technology")
        self.assertTrue(data3["ready"])

    def test_add_stock_endpoint_candidate(self):
        payload = {
            "symbol": "AMD",
            "archetype": "large_cap_tech",
            "group": "candidate",
            "skip_validation": True,
        }
        resp = self.client.post("/api/universe/add", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["symbol"], "AMD")
        self.assertEqual(data["archetype"], "large_cap_tech")

        # Verify persisted in watchers.json
        watchers = json.loads(self.watchers_json.read_text(encoding="utf-8"))
        symbols = [w["symbol"] for w in watchers["new_watchers"]]
        self.assertIn("AMD", symbols)
        amd = next(w for w in watchers["new_watchers"] if w["symbol"] == "AMD")
        self.assertEqual(amd["archetype"], "large_cap_tech")

    def test_add_stock_duplicate(self):
        payload = {
            "symbol": "MRVL",
            "archetype": "default",
            "group": "candidate",
            "skip_validation": True,
        }
        resp = self.client.post("/api/universe/add", json=payload)
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("already enrolled", data["message"])

    def test_remove_stock_endpoint(self):
        resp = self.client.post("/api/universe/remove", json={"symbol": "TSEM"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

        watchers = json.loads(self.watchers_json.read_text(encoding="utf-8"))
        self.assertNotIn("TSEM", [w["symbol"] for w in watchers["new_watchers"]])


if __name__ == "__main__":
    unittest.main()
