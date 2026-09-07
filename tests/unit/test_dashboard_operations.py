import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from traderbot.cli import dashboard
from traderbot.dashboard.operations import Operations, TAIL_BYTES

NOW = dt.datetime(2026, 9, 6, 16, tzinfo=dt.timezone.utc)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ('config', 'runtime/logs', 'runtime/state'):
            (self.root / directory).mkdir(parents=True)
        self.ops = Operations(self.root, lambda: NOW)
        self.log = self.root / 'runtime/logs/abc.jsonl'
        self.state = self.root / 'runtime/state/abc.json'
        self.config = self.root / 'config/watchers.json'
        self.config.write_text(json.dumps({'watchers': [{'symbol': 'ABC', 'log': 'runtime/logs/abc.jsonl', 'state': 'runtime/state/abc.json'}]}))
        self.state.write_text(json.dumps({'dynamic_entry_plan': {'setup_score': 85, 'status': 'watch', 'last_bar_time': '2026-09-04T19:00:00Z'}}))

    def record(self, minutes=10, **overrides):
        return {'timestamp': (NOW-dt.timedelta(minutes=minutes)).isoformat(), 'failures': 0,
                'next_run_seconds': 900, 'result': {'status': 'market_closed_sleeping'}, **overrides}

    def write(self, records):
        self.log.write_text(''.join(json.dumps(r)+'\n' for r in records))

    def test_closed_poll_and_overdue_watchers(self):
        self.write([self.record()])
        self.assertEqual(self.ops.collect(set())['watchers'][0]['status'], 'Within schedule')
        self.write([self.record(minutes=21)])
        self.assertEqual(self.ops.collect(set())['watchers'][0]['status'], 'Overdue')
        self.assertEqual(self.ops.collect(set())['process_status'], 'Not verified')

    def test_partial_lines_bad_json_and_secret_fields(self):
        self.write([self.record(result={'status':'temporary_error','error':'SECRET'}, failures=1)])
        with self.log.open('a') as f:
            f.write('garbage\n{"partial":')
        data=self.ops.collect(set())
        self.assertEqual(len(data['activity']),1)
        self.assertTrue(data['issues'])
        self.assertEqual(data['alerts'][0]['kind'],'Watcher error')
        self.assertNotIn('SECRET',json.dumps(data))

    def test_state_partial_write_retains_candidate_then_recovers(self):
        self.write([self.record()])
        self.assertEqual(self.ops.collect(set())['candidates'][0]['score'],85)
        self.state.write_text('{')
        retained=self.ops.collect(set())['candidates'][0]
        self.assertEqual(retained['score'],85)
        self.assertIn('unavailable',retained['source_status'])
        self.state.write_text('{}')
        self.assertEqual(self.ops.collect(set())['candidates'],[])

    def test_rotation_missing_and_empty_file(self):
        self.write([self.record()])
        self.ops.collect(set())
        self.log.rename(self.log.with_suffix('.old'))
        self.assertEqual(self.ops.collect(set())['watchers'][0]['status'],'Source unavailable')
        self.write([self.record(minutes=1)])
        self.assertEqual(self.ops.collect(set())['watchers'][0]['status'],'Within schedule')
        self.log.write_text('')
        self.assertEqual(self.ops.collect(set())['activity'],[])

    def test_bounded_log_tail_and_future_record(self):
        self.log.write_bytes(b'x'*(TAIL_BYTES*3)+b'\n'+json.dumps(self.record()).encode()+b'\n'+json.dumps(self.record(minutes=-1)).encode()+b'\n')
        self.assertEqual(len(self.ops.collect(set())['activity']),1)
        self.assertEqual(len(self.ops.files[self.log]['data']),2)

    def test_held_disabled_and_unknown_watchers(self):
        self.assertEqual(self.ops.collect({'ABC'})['candidates'],[])
        self.assertEqual(self.ops.collect(set())['watchers'][0]['status'],'Source unavailable')
        config=json.loads(self.config.read_text());config['watchers'][0]['enabled']=False
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.ops.collect(set())['watchers'][0]['status'],'Disabled')

    def test_cache_shared_between_tabs_and_paths_confined(self):
        with patch.object(self.ops,'collect',wraps=self.ops.collect) as collect:
            self.ops.snapshot([]);self.ops.snapshot([])
            self.assertEqual(collect.call_count,1)
        outside=self.root/'secret.json';outside.write_text('{"SECRET":1}')
        value,issue=self.ops.read(outside)
        self.assertEqual(value,{})
        self.assertTrue(issue)

    def test_holdings_change_invalidates_candidate_cache(self):
        self.assertEqual(len(self.ops.snapshot([])['candidates']), 1)
        self.assertEqual(self.ops.snapshot(['ABC'])['candidates'], [])
        self.assertEqual(len(self.ops.snapshot([])['candidates']), 1)

    def test_closed_market_fallback_and_invalid_timestamp(self):
        self.write([self.record(next_run_seconds=None)])
        self.assertEqual(self.ops.collect(set())['watchers'][0]['status'], 'Within schedule')
        self.write([self.record(), self.record(timestamp='bad timestamp')])
        data = self.ops.collect(set())
        self.assertEqual(data['watchers'][0]['status'], 'Source unavailable')
        self.assertEqual(len(data['activity']), 1)
        self.assertTrue(data['issues'])

    def test_alert_sources_are_timestamped_and_allowlisted(self):
        for filename in ('position_health_alerts.jsonl', 'watcher_monitor_alerts.jsonl'):
            (self.root / 'runtime/logs' / filename).write_text(json.dumps({
                'timestamp': NOW.isoformat(), 'symbol': 'ABC', 'state': 'warning',
                'recommended_action': 'review', 'message': 'SECRET',
            }) + '\n')
        alerts = self.ops.collect(set())['alerts']
        self.assertEqual({a['source'] for a in alerts}, {'Position health', 'Watchdog'})
        self.assertTrue(all(a['timestamp'] == NOW.isoformat() for a in alerts))
        self.assertNotIn('SECRET', json.dumps(alerts))


if __name__=='__main__':
    unittest.main()
