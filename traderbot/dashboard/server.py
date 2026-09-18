"""Phase 1: serve selected fields from saved reports, without broker access."""
import datetime as dt
import json
import math
import re
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"
ACCOUNT_FIELDS = ("equity", "cash", "buying_power", "day_gain_percent")
POSITION_NUMBERS = (
    "qty", "avg_entry_price", "current_price", "market_value", "total_gain_loss",
    "total_gain_loss_percent", "position_health_score", "position_health_downside_score",
    "position_health_trend_score", "position_health_reward_risk_score",
    "position_health_stop_price", "position_health_stop_qty",
)
POSITION_TEXT = ("symbol", "position_health_state", "position_health_action", "position_health_as_of")
ASSETS = ("app.js", "style.css", "vendor/react.js", "vendor/react-dom.js")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class SnapshotStore:
    def __init__(self, root=ROOT):
        self.directory = Path(root) / "runtime/reports/daily"

    def snapshot(self):
        data = {"account": {}, "positions": [], "source": "Saved report",
                "report_date": None, "report_as_of": None, "report_file": None,
                "status": "missing", "warning": "No saved report is available. Generate a daily report first."}
        reports = sorted(path for path in self.directory.glob("daily_report_*.json")
                         if re.fullmatch(r"daily_report_\d{4}-\d{2}-\d{2}\.json", path.name))
        if not reports:
            return data
        path = reports[-1]
        try:
            if path.is_symlink() or path.resolve().parent != self.directory.resolve():
                raise ValueError("Invalid report path")
            with path.open(encoding="utf-8") as stream:
                report = json.load(stream)
            if not isinstance(report, dict) or not isinstance(report.get("account"), dict) or not isinstance(report.get("current_positions"), list):
                raise ValueError("Invalid report schema")
            data.update(report_file=path.name, report_date=path.stem.removeprefix("daily_report_"),
                        report_as_of=dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat())
            data["account"] = {key: number(report["account"].get(key)) for key in ACCOUNT_FIELDS}
            for position in report["current_positions"]:
                if not isinstance(position, dict) or not isinstance(position.get("symbol"), str):
                    raise ValueError("Invalid position schema")
                item = {key: number(position.get(key)) for key in POSITION_NUMBERS}
                item.update({key: position.get(key) if isinstance(position.get(key), str) else None for key in POSITION_TEXT})
                for key in ("position_health_data_fresh", "position_health_data_complete"):
                    item[key] = position.get(key) is True
                reasons = position.get("position_health_reasons")
                item["position_health_reasons"] = [v for v in reasons if isinstance(v, str)] if isinstance(reasons, list) else []
                data["positions"].append(item)
            data.update(status="available", warning=(
                "The saved report records an account-data error; values may be incomplete."
                if report.get("account_error") else None))
        except (OSError, ValueError):
            data.update(account={}, positions=[], status="unavailable",
                        warning="The latest report could not be read. It may be incomplete or being updated. Try Refresh report.")
        return data


def create_app(store=None):
    store = store or SnapshotStore()
    @asynccontextmanager
    async def lifespan(app):
        if hasattr(store, "start"):
            store.start()
        yield
        if hasattr(store, "close"):
            store.close()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/snapshot")
    def snapshot():
        return store.snapshot()

    if hasattr(store, "start"):
        @app.get("/api/events")
        async def events(request: Request):
            async def stream():
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    snapshot = await asyncio.to_thread(store.snapshot)
                    yield "data: " + json.dumps(snapshot, allow_nan=False) + "\n\n"
                    await asyncio.sleep(snapshot.get("stream_interval_seconds", 5))
            return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    def endpoint_factory(path):
        def endpoint():
            return FileResponse(path)
        return endpoint

    for name in ASSETS:
        app.add_api_route("/static/" + name, endpoint_factory(STATIC / name), methods=["GET"])
    return app


def main(*, log_config=None):
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="Read-only TraderBot dashboard.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--offline", action="store_true", help="Read saved reports only; do not connect to the broker.")
    args = parser.parse_args()
    from traderbot.dashboard.live import LiveStore
    store = SnapshotStore() if args.offline else LiveStore(ROOT)
    uvicorn.run(create_app(store), host="127.0.0.1", port=args.port,
                log_config=log_config or uvicorn.config.LOGGING_CONFIG)
