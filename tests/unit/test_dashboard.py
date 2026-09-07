import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traderbot.cli import dashboard  # Loads the isolated optional dependencies.
from fastapi.testclient import TestClient
from traderbot.dashboard.server import SnapshotStore, create_app


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "runtime/reports/daily"
        self.directory.mkdir(parents=True)
        self.store = SnapshotStore(self.root)
        self.client = TestClient(create_app(self.store), base_url="http://127.0.0.1")

    def report(self, date="2026-09-04", **overrides):
        report = {"account": {"equity": "50218.95", "cash": 43441.37, "secret": "SECRET"},
                  "current_positions": [{"symbol": "ORCL", "qty": 12, "position_health_score": 100,
                                         "position_health_as_of": "2026-09-04T19:00:00Z", "secret": "SECRET"}],
                  **overrides}
        path = self.directory / f"daily_report_{date}.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_values_and_allowlist_without_network_or_writes(self):
        path = self.report()
        before = path.read_bytes()
        with patch("socket.socket.connect", side_effect=AssertionError("Network access")):
            self.assertEqual(self.store.snapshot()["status"], "available")
        response = self.client.get("/api/snapshot")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"]["equity"], 50218.95)
        self.assertEqual(response.json()["positions"][0]["position_health_score"], 100)
        self.assertNotIn("SECRET", response.text)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list(self.root.rglob("*.*"))), 1)

    def test_missing_empty_and_bad_reports(self):
        self.assertEqual(self.store.snapshot()["status"], "missing")
        path = self.report(current_positions=[])
        self.assertEqual(self.store.snapshot()["status"], "available")
        self.assertEqual(self.store.snapshot()["positions"], [])
        for body in ('{"account":', 'null', '[]', '{"account":{},"current_positions":[null]}'):
            path.write_text(body, encoding="utf-8")
            self.assertEqual(self.store.snapshot()["status"], "unavailable")

    def test_refresh_reads_latest_report_and_replacement(self):
        self.report()
        path = self.report("2026-09-08", account={"equity": 123})
        self.assertEqual(self.store.snapshot()["report_date"], "2026-09-08")
        self.assertEqual(self.store.snapshot()["account"]["equity"], 123)
        path.write_text('{"account":{"equity":456},"current_positions":[]}', encoding="utf-8")
        self.assertEqual(self.store.snapshot()["account"]["equity"], 456)

    def test_invalid_numbers_and_account_errors_are_safe(self):
        self.report(account={"equity": "NaN", "cash": "Infinity"}, account_error="SECRET request URL")
        response = self.client.get("/api/snapshot")
        self.assertIsNone(response.json()["account"]["equity"])
        self.assertNotIn("SECRET", response.text)
        self.assertTrue(response.json()["warning"])

    def test_only_explicit_read_routes(self):
        for url in ("/.env", "/static/../server.py", "/static/vendor/LICENSE-react.txt", "/api/events", "/config/watchers.json"):
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post("/api/snapshot").status_code, 405)
        self.assertEqual(self.client.get("/", headers={"host": "evil.example"}).status_code, 400)
        response = self.client.get("/static/app.js?path=.env")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ReactDOM", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
