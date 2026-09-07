import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from traderbot.cli import dashboard
from traderbot.dashboard.live import LiveStore, ReadOnlyBroker

NOW = dt.datetime(2026, 9, 6, 16, tzinfo=dt.timezone.utc)


class Broker:
    def __init__(self):
        self.calls = []
        self.failed = set()
        self.positions = [{"symbol": "ORCL", "qty": "12", "avg_entry_price": "145.83", "current_price": "158.78", "unrealized_plpc": ".0888"}]

    def get(self, path):
        self.calls.append(path)
        if path in self.failed:
            raise ValueError("SECRET credential URL")
        if path == "/account":
            return {"equity": "50218.95", "last_equity": "50000", "cash": "43441.37", "non_marginable_buying_power": "46505.68", "secret": "SECRET"}
        if path == "/positions":
            return self.positions
        if path == "/clock":
            return {"is_open": False, "next_open": "2026-09-08T09:30:00-04:00"}
        return [{"symbol": "ORCL", "status": "filled", "secret": "SECRET"}]

    def session_close(self, now):
        return "2026-09-04T16:00:00-04:00"


class LiveDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = Broker()
        self.now = NOW
        self.store = LiveStore(self.root, self.broker, clock=lambda: self.now)

    def test_shared_cache_many_tabs_and_no_sensitive_fields(self):
        self.store.refresh()
        with ThreadPoolExecutor(8) as pool:
            snapshots = list(pool.map(lambda _: self.store.snapshot(), range(30)))
        self.assertEqual(len(self.broker.calls), 4)
        self.assertEqual(snapshots[0]["account"]["equity"], 50218.95)
        self.assertNotIn("SECRET", json.dumps(snapshots))
        snapshots[0]["positions"].clear()
        self.assertEqual(len(self.store.snapshot()["positions"]), 1)

    def test_partial_failure_retains_section_and_recovery_clears_stale(self):
        self.store.refresh()
        before = self.store.snapshot()["connection"]["positions"]["as_of"]
        self.now += dt.timedelta(seconds=20)
        self.broker.failed.add("/positions")
        self.store.refresh()
        data = self.store.snapshot()
        self.assertTrue(data["connection"]["positions"]["stale"])
        self.assertFalse(data["connection"]["account"]["stale"])
        self.assertEqual(data["connection"]["positions"]["as_of"], before)
        self.assertEqual(len(data["positions"]), 1)
        self.assertNotIn("SECRET", json.dumps(data))
        self.broker.failed.clear()
        self.broker.positions = []
        self.store.refresh()
        self.assertEqual(self.store.snapshot()["positions"], [])
        self.assertFalse(self.store.snapshot()["connection"]["positions"]["stale"])

    def test_age_becomes_stale_without_a_refresh(self):
        self.store.refresh()
        self.now += dt.timedelta(seconds=61)
        self.assertTrue(self.store.snapshot()["connection"]["account"]["stale"])

    def health_files(self):
        reports = self.root / "runtime/reports/daily"
        reports.mkdir(parents=True)
        p = {"symbol": "ORCL", "qty": 12, "avg_entry_price": 145.83, "position_health_score": 100,
             "position_health_state": "Healthy", "position_health_as_of": "2026-09-04T19:00:00Z",
             "position_health_data_complete": True, "position_health_data_fresh": True}
        path = reports / "daily_report_2026-09-04.json"
        path.write_text(json.dumps({"account": {}, "current_positions": [p]}))
        os.utime(path, (NOW.timestamp()-60, NOW.timestamp()-60))
        (self.root / "config").mkdir()
        (self.root / "config/watchers.json").write_text(json.dumps({"managed_watchers": [{"symbol": "ORCL", "state": "runtime/state/orcl.json"}]}))
        state_dir = self.root / "runtime/state"
        state_dir.mkdir()
        state = {"position_episode": {"average_entry_price": 145.83},
                 "position_health_last_evaluated_at": "2026-09-06T11:58:00-04:00",
                 "position_health": {"position_qty": 12, "score": 43, "state": "At Risk", "as_of": "2026-09-04T19:00:00Z", "data_complete": True, "data_fresh": True}}
        state_path = state_dir / "orcl.json"
        state_path.write_text(json.dumps(state))
        return state_path, state

    def test_closed_market_health_and_ordering_use_evaluation_time(self):
        path, state = self.health_files()
        self.store.refresh()
        p = self.store.snapshot()["positions"][0]
        self.assertEqual(p["position_health_score"], 100)
        self.assertTrue(p["position_health_current"])
        state["position_health_last_evaluated_at"] = "2026-09-06T11:59:30-04:00"
        path.write_text(json.dumps(state))
        self.store.refresh()
        self.assertEqual(self.store.snapshot()["positions"][0]["position_health_score"], 43)

    def test_changed_holding_does_not_inherit_health(self):
        self.health_files()
        self.broker.positions[0]["avg_entry_price"] = "155"
        self.store.refresh()
        p = self.store.snapshot()["positions"][0]
        self.assertNotIn("position_health_score", p)
        self.assertFalse(p["position_health_current"])

    def test_fresh_prices_do_not_make_old_health_current(self):
        self.health_files()
        self.now = dt.datetime(2026, 9, 8, 18, tzinfo=dt.timezone.utc)
        self.broker.session_close = lambda now: None
        self.store.refresh()
        p = self.store.snapshot()["positions"][0]
        self.assertEqual(p["position_health_score"], 100)
        self.assertFalse(p["position_health_current"])
        self.assertFalse(self.store.snapshot()["connection"]["positions"]["stale"])

    def test_missing_credentials_keeps_saved_snapshot_and_sanitizes_error(self):
        self.store.broker = None
        def fail(root):
            self.store.stop.set()
            raise KeyError("SECRET")
        with patch("traderbot.dashboard.live.ReadOnlyBroker", side_effect=fail):
            self.store.run()
        data = self.store.snapshot()
        self.assertTrue(data["connection"]["account"]["stale"])
        self.assertNotIn("SECRET", json.dumps(data))

    def test_read_only_adapter_rejects_unlisted_endpoint(self):
        adapter = object.__new__(ReadOnlyBroker)
        with self.assertRaises(ValueError):
            adapter.get("/orders/new")


if __name__ == "__main__":
    unittest.main()
