"""Browser smoke checks against the running saved-report viewer."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "runtime/dashboard-test-deps"), str(ROOT / "runtime/dashboard-deps")]
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors, external = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: external.append(request.url) if not request.url.startswith("http://127.0.0.1:8765/") else None)
        page.goto("http://127.0.0.1:8765/")
        page.get_by_role("heading", name="Positions & health").wait_for()
        snapshot = page.request.get("http://127.0.0.1:8765/api/snapshot").json()
        if snapshot["positions"]:
            symbol = snapshot["positions"][0]["symbol"]
            page.get_by_label("Search positions").fill(symbol)
            assert page.locator("tbody tr").count() == 1
            page.get_by_text("Score details", exact=True).click()
            assert page.locator("details[open]").count() == 1
            page.get_by_label("Search positions").fill("NO_MATCH_123")
            assert page.get_by_text("No positions match this filter.").is_visible()
            page.get_by_label("Search positions").fill("")
            page.get_by_label("Filter position health").select_option("risk")
            expected = sum(p.get("position_health_state") in (None, "At Risk", "Critical", "Unprotected", "Unavailable") for p in snapshot["positions"])
            if expected:
                assert page.locator("tbody tr").count() == expected
            page.get_by_label("Filter position health").select_option("all")
        page.screenshot(path=str(ROOT / "runtime/dashboard-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(ROOT / "runtime/dashboard-mobile.png"), full_page=True)
        # Simulate absent report and a failed refresh without changing real files.
        missing = {**snapshot, "account": {}, "positions": [], "status": "missing", "warning": "No saved report is available."}
        page.route("**/api/snapshot", lambda route: route.fulfill(json=missing))
        page.get_by_role("button", name="Refresh report").click()
        page.get_by_text("No saved report is available.", exact=True).wait_for()
        page.unroute("**/api/snapshot")
        page.route("**/api/snapshot", lambda route: route.abort())
        page.get_by_role("button", name="Refresh report").click()
        page.get_by_text("Refresh failed. Showing the previously loaded snapshot.").wait_for()
        assert not errors, errors
        assert not external, external
        browser.close()
        print("Browser checks passed: report rendering, search, details, filters, mobile width, missing report, failed refresh, no external requests.")


if __name__ == "__main__":
    main()
