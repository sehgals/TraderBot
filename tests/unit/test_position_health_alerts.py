import json
import tempfile
import unittest
from pathlib import Path

from traderbot.monitoring.supervisor import (
    POSITION_HEALTH_ALERT_LOG,
    record_position_health_alert,
)


class PositionHealthAlertTests(unittest.TestCase):
    def test_critical_health_alert_is_written_once_per_assessment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = {"symbol": "WAT"}
            result = {
                "position_health": {
                    "state": "Critical",
                    "score": 12,
                    "recommended_action": "exit",
                    "bar_id": "bar-1",
                    "as_of": "2026-08-11T15:00:00Z",
                    "reasons": ["structural_trend_failure"],
                }
            }

            first = record_position_health_alert(root, watcher, result)
            second = record_position_health_alert(root, watcher, result)

            self.assertIsNotNone(first)
            self.assertIsNone(second)
            lines = (root / POSITION_HEALTH_ALERT_LOG).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["symbol"], "WAT")

    def test_healthy_assessment_clears_alert_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            watcher = {"symbol": "WAT", "last_health_alert_key": ("old",)}

            result = record_position_health_alert(
                Path(directory),
                watcher,
                {"position_health": {"state": "Healthy", "reasons": []}},
            )

            self.assertIsNone(result)
            self.assertNotIn("last_health_alert_key", watcher)


if __name__ == "__main__":
    unittest.main()
