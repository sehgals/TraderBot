"""Exercise SSE with two tabs and offline/reconnect using a fake read-only broker."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "runtime/dashboard-test-deps"), str(ROOT / "runtime/dashboard-deps")]
from playwright.sync_api import sync_playwright
import uvicorn
from traderbot.dashboard.live import LiveStore
from traderbot.dashboard.server import create_app


class Broker:
    equity = "12345"
    calls = 0

    def get(self, path):
        self.calls += 1
        if path == "/account":
            return {"equity": self.equity, "last_equity": "12000"}
        if path == "/clock":
            return {"is_open": False}
        return []

    def session_close(self, now):
        return None


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for folder in ("config", "runtime/logs", "runtime/state"):
            (root / folder).mkdir(parents=True)
        (root / "config/watchers.json").write_text(json.dumps({"watchers": [{"symbol": "TEST", "log": "runtime/logs/test.jsonl", "state": "runtime/state/test.json"}]}))
        (root / "runtime/logs/test.jsonl").write_text(json.dumps({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "next_run_seconds": 900, "result": {"status": "market_closed_sleeping"}}) + "\n")
        (root / "runtime/state/test.json").write_text(json.dumps({"dynamic_entry_plan": {"setup_score": 85, "status": "watch", "blockers": ["market_ok"]}}))
        broker = Broker()
        store = LiveStore(Path(directory), broker)
        store.interval = 3600  # Browser traffic must not trigger broker calls.
        server = uvicorn.Server(uvicorn.Config(create_app(store), host="127.0.0.1", port=8767, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if time.monotonic() > deadline:
                    raise TimeoutError("Test server did not start")
                time.sleep(.05)
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context()
                pages = [context.new_page(), context.new_page()]
                errors = []
                for page in pages:
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto("http://127.0.0.1:8767")
                    page.get_by_text("Updates connected", exact=True).wait_for()
                    page.get_by_text("$12,345.00", exact=True).wait_for()
                assert broker.calls == 4, broker.calls
                broker.equity = "13579"
                store.refresh()
                for page in pages:
                    page.get_by_text("$13,579.00", exact=True).wait_for(timeout=12000)
                context.set_offline(True)
                pages[0].get_by_text("Reconnecting updates", exact=True).wait_for(timeout=12000)
                context.set_offline(False)
                pages[0].get_by_text("Updates connected", exact=True).wait_for(timeout=15000)
                assert broker.calls == 8, broker.calls
                page = pages[0]
                page.get_by_role("heading", name="Watcher operations", exact=True).wait_for()
                page.get_by_label("Filter operations by symbol").fill("NO_MATCH")
                page.get_by_text("No candidate assessments match this filter.", exact=True).wait_for()
                page.get_by_label("Filter operations by symbol").fill("TEST")
                page.get_by_text("Within schedule", exact=True).wait_for()
                page.get_by_label("Show watchers needing attention").check()
                page.get_by_text("No watchers match, or watcher configuration is unavailable.", exact=True).wait_for()
                page.get_by_label("Show watchers needing attention").uncheck()
                page.get_by_text("Blockers", exact=True).click()
                page.get_by_text("market ok", exact=True).wait_for()
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                pages[0].screenshot(path=str(ROOT / "runtime/dashboard-live-test.png"), full_page=True)
                assert not errors, errors
                browser.close()
            print("Live browser checks passed: shared refresh, SSE, reconnect, operations filters, candidate blockers, empty states, mobile width.")
        finally:
            server.should_exit = True
            store.close()
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
