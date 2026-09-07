"""Isolated Phase 5 browser acceptance: no credentials or production reports."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'runtime/dashboard-test-deps'), str(ROOT / 'runtime/dashboard-deps')]
from playwright.sync_api import sync_playwright, expect
import uvicorn
from traderbot.dashboard.server import SnapshotStore, create_app


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reports = root / 'runtime/reports/daily'
        reports.mkdir(parents=True)
        position = dict(symbol='TEST', qty=12, avg_entry_price=100, current_price=110,
                        market_value=1320, total_gain_loss=120, total_gain_loss_percent=10,
                        position_health_state='At Risk', position_health_score=42,
                        position_health_action='review', position_health_stop_price=95,
                        position_health_stop_qty=12, position_health_as_of='2026-09-04T19:00:00Z',
                        position_health_reasons=['trend_weakening'])
        (reports / 'daily_report_2026-09-04.json').write_text(json.dumps(dict(
            account=dict(equity=12345, cash=11025, buying_power=11025),
            current_positions=[position, {**position, 'symbol': 'DEMO', 'position_health_state': 'Healthy'}])))
        store = SnapshotStore(root)
        server = uvicorn.Server(uvicorn.Config(create_app(store), host='127.0.0.1', port=8768, log_level='error'))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise TimeoutError('Release test server did not start')
                time.sleep(.05)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors, external, pending = [], [], []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: external.append(r.url) if not r.url.startswith('http://127.0.0.1:8768/') else None)
                page.route('**/api/snapshot', lambda route: pending.append(route))
                page.goto('http://127.0.0.1:8768')
                expect(page.get_by_role('status')).to_have_text('Loading dashboard...')
                assert pending
                pending.pop().fulfill(json=store.snapshot())
                page.unroute('**/api/snapshot')
                expect(page.get_by_role('heading', name='Positions & health')).to_be_visible()
                # Traverse the actual tab order, including the scrollable region.
                page.keyboard.press('Tab')
                expect(page.get_by_role('button', name='Refresh report')).to_be_focused()
                page.keyboard.press('Tab')
                expect(page.get_by_label('Search positions')).to_be_focused()
                page.keyboard.type('TEST')
                expect(page.locator('tbody tr')).to_have_count(1)
                page.keyboard.press('Tab')
                expect(page.get_by_label('Filter position health')).to_be_focused()
                page.keyboard.press('Tab')
                expect(page.get_by_role('region')).to_be_focused()
                page.keyboard.press('Tab')
                expect(page.get_by_text('Score details', exact=True)).to_be_focused()
                page.keyboard.press('Enter')
                expect(page.get_by_text('trend weakening', exact=True)).to_be_visible()
                page.get_by_label('Search positions').fill('')
                page.get_by_label('Filter position health').select_option('risk')
                expect(page.locator('tbody tr')).to_have_count(1)
                page.get_by_label('Filter position health').select_option('all')
                page.screenshot(path=str(ROOT / 'runtime/dashboard-release-desktop.png'), full_page=True)
                for width in (320, 390, 768):
                    page.set_viewport_size({'width': width, 'height': 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.set_viewport_size({'width': 390, 'height': 844})
                region = page.get_by_role('region')
                region.focus()
                page.keyboard.press('ArrowRight')
                page.wait_for_timeout(250)  # Allow the native key-scroll animation.
                assert region.evaluate('(element) => element.scrollLeft > 0')
                page.screenshot(path=str(ROOT / 'runtime/dashboard-release-mobile.png'), full_page=True)
                # Print expands all details and restores the user's choices afterwards.
                page.set_viewport_size({'width': 1440, 'height': 1000})
                before = page.locator('details[open]').count()
                page.emulate_media(media='print')
                page.evaluate("dispatchEvent(new Event('beforeprint'))")
                expect(page.locator('details[open]')).to_have_count(2)
                expect(page.get_by_role('button', name='Refresh report')).to_be_hidden()
                assert page.evaluate("getComputedStyle(document.querySelector('.pill')).color") == 'rgb(17, 17, 17)'
                assert page.evaluate("document.querySelector('table').scrollWidth <= document.querySelector('table').clientWidth")
                page.screenshot(path=str(ROOT / 'runtime/dashboard-release-print.png'), full_page=True)
                page.evaluate("dispatchEvent(new Event('afterprint'))")
                expect(page.locator('details[open]')).to_have_count(before)
                page.emulate_media(media='screen')
                missing = {**store.snapshot(), 'account': {}, 'positions': [],
                           'status': 'missing', 'warning': 'No saved report is available.'}
                page.route('**/api/snapshot', lambda route: route.fulfill(json=missing))
                page.get_by_role('button', name='Refresh report').click()
                expect(page.get_by_text('No saved report is available.', exact=True)).to_be_visible()
                expect(page.get_by_text('Positions unavailable until a valid report is loaded.')).to_be_visible()
                page.unroute('**/api/snapshot')
                # Stale live sections preserve visible values; no broker connection.
                stale = {**store.snapshot(), 'mode': 'live', 'orders': [], 'market': {},
                         'connection': {k: {'as_of': '2026-09-04T19:00:00Z', 'stale': True} for k in ('account', 'positions', 'orders', 'market')}}
                page.route('**/api/events', lambda route: route.abort())
                page.route('**/api/snapshot', lambda route: route.fulfill(json=stale))
                page.get_by_role('button', name='Refresh report').click()
                expect(page.get_by_text('Stale / unavailable', exact=True)).to_have_count(4)
                expect(page.get_by_text('$12,345.00', exact=True)).to_be_visible()
                expect(page.get_by_text('Stale / missing assessment', exact=True)).to_have_count(2)
                page.screenshot(path=str(ROOT / 'runtime/dashboard-release-stale.png'), full_page=True)
                page.unroute('**/api/snapshot')
                page.route('**/api/snapshot', lambda route: route.abort())
                page.get_by_role('button', name='Refresh view').click()
                expect(page.get_by_text('Refresh failed. Showing the previously loaded snapshot.')).to_be_visible()
                page.reload()
                expect(page.get_by_role('button', name='Retry')).to_be_visible()
                page.unroute('**/api/snapshot')
                page.get_by_role('button', name='Retry').click()
                expect(page.get_by_role('heading', name='Positions & health')).to_be_visible()
                assert not errors, errors
                assert not external, external
                browser.close()
            print('Release browser checks passed: loading, keyboard navigation/scroll/details, filters, 320/390/768px widths, print expansion/restoration, stale values, failed refresh, no external requests.')
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == '__main__':
    main()
