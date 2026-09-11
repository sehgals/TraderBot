"""One read-only broker collector shared by every dashboard connection."""
import copy
import datetime as dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from traderbot.dashboard.server import SnapshotStore, number, POSITION_NUMBERS, POSITION_TEXT

UTC = dt.timezone.utc


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


class ReadOnlyBroker:
    def __init__(self, root):
        from traderbot.core_strategy_engine.engine import AlpacaClient, load_env
        load_env(root / ".env")
        self.client = AlpacaClient()

    def get(self, path):
        # Fixed read endpoints only; no execution gateway or strategy evaluation.
        if path not in ("/account", "/positions", "/clock", "/orders?status=all&limit=20&direction=desc"):
            raise ValueError("Unsupported dashboard endpoint")
        return self.client.request("GET", self.client.trade_base_url + path, retries=1)

    def session_close(self, now):
        from traderbot.core_strategy_engine.engine import position_health_session_close
        return position_health_session_close(self, now)

    def calendar(self, start, end):
        # Dates are generated internally; one attempt keeps refresh duration bounded.
        dt.date.fromisoformat(start)
        dt.date.fromisoformat(end)
        return self.client.request("GET", self.client.trade_base_url + f"/calendar?start={start}&end={end}", retries=1)


def account_data(raw):
    if not isinstance(raw, dict) or number(raw.get("equity")) is None:
        raise ValueError("Invalid account")
    equity, prior = number(raw["equity"]), number(raw.get("last_equity"))
    return {"equity": equity, "cash": number(raw.get("cash")),
            "buying_power": number(raw.get("non_marginable_buying_power")),
            "day_gain": equity - prior if prior else None,
            "day_gain_percent": (equity / prior - 1) * 100 if prior else None}


def positions_data(raw):
    if not isinstance(raw, list):
        raise ValueError("Invalid positions")
    result = []
    for p in raw:
        if not isinstance(p, dict) or not isinstance(p.get("symbol"), str) or number(p.get("qty")) is None:
            raise ValueError("Invalid position")
        row = {key: number(p.get(key)) for key in ("qty", "avg_entry_price", "current_price", "market_value")}
        row.update(symbol=p["symbol"], total_gain_loss=number(p.get("unrealized_pl")),
                   total_gain_loss_percent=number(p.get("unrealized_plpc")))
        if row["total_gain_loss_percent"] is not None:
            row["total_gain_loss_percent"] *= 100
        result.append(row)
    return result


def market_data(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("is_open"), bool):
        raise ValueError("Invalid clock")
    return {key: raw.get(key) for key in ("is_open", "next_open", "next_close", "timestamp")}


def orders_data(raw):
    if not isinstance(raw, list) or any(not isinstance(order, dict) for order in raw):
        raise ValueError("Invalid orders")
    keys = ("symbol", "side", "qty", "filled_qty", "status", "type", "submitted_at")
    return [{k: o.get(k) if isinstance(o.get(k), (str, int, float)) else None for k in keys} for o in raw[:20]]


class LiveStore:
    interval = 20

    def __init__(self, root, broker=None, clock=None):
        self.root, self.broker = root, broker
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.reports = SnapshotStore(root)
        from traderbot.dashboard.operations import Operations
        self.operations = Operations(root, clock=self.clock)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.data = self.reports.snapshot()
        self.data.update(mode="live", orders=[], market={}, connection={}, session_close=None)
        for section in ("account", "positions", "orders", "market", "calendar"):
            self.data["connection"][section] = {"as_of": None, "error": "Waiting for first refresh"}

    def start(self):
        if self.worker is None:
            self.worker = threading.Thread(target=self.run, daemon=True, name="dashboard-collector")
            self.worker.start()

    def close(self):
        self.stop.set()

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                if self.broker is None:
                    self.broker = ReadOnlyBroker(self.root)
                self.refresh()
            except Exception:
                with self.lock:
                    for section in self.data["connection"].values():
                        section["error"] = "Broker unavailable; check configuration or connectivity"
            self.stop.wait(max(1, self.interval - (time.monotonic() - started)))

    def refresh(self):
        tasks = {
            "account": ("/account", account_data),
            "positions": ("/positions", positions_data),
            "orders": ("/orders?status=all&limit=20&direction=desc", orders_data),
            "market": ("/clock", market_data),
        }
        with self.lock:
            data = copy.deepcopy(self.data)
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {key: pool.submit(self.broker.get, path) for key, (path, _) in tasks.items()}
            calendar = pool.submit(self.broker.session_close, self.clock())
            for key, future in futures.items():
                try:
                    data[key] = tasks[key][1](future.result())
                    data["connection"][key] = {"as_of": self.clock().isoformat(), "error": None}
                except Exception:
                    data["connection"][key]["error"] = "Refresh failed; showing last-known values"
            try:
                data["session_close"] = calendar.result()
                data["connection"]["calendar"] = {"as_of": self.clock().isoformat(), "error": None}
            except Exception:
                data["connection"]["calendar"]["error"] = "Calendar unavailable"
                data["session_close"] = None
        if data["connection"]["positions"]["as_of"]:
            data["status"] = "available"
        self.merge_health(data)
        with self.lock:
            self.data = data

    def merge_health(self, data):
        report = self.reports.snapshot()
        report_positions = {p["symbol"]: p for p in report["positions"]}
        config = read_object(self.root / "config/watchers.json")
        managed = config.get("managed_watchers", config.get("watchers", []))
        new = config.get("new_watchers", [])
        watchers = (managed if isinstance(managed, list) else []) + (new if isinstance(new, list) else [])
        states = {}
        for watcher in watchers:
            if not isinstance(watcher, dict) or not isinstance(watcher.get("state"), str):
                continue
            path = self.root / watcher.get("state", "")
            if path.resolve().is_relative_to((self.root / "runtime").resolve()):
                states[watcher.get("symbol")] = read_object(path)
        now = self.clock()
        for position in data["positions"]:
            candidates = []
            saved = report_positions.get(position["symbol"], {})
            # Do not reuse an assessment for a changed or reopened holding.
            if saved.get("qty") == position.get("qty") and saved.get("avg_entry_price") == position.get("avg_entry_price"):
                candidates.append((saved, "Saved report", report.get("report_as_of")))
            state = states.get(position["symbol"], {})
            health = state.get("position_health") or {}
            episode = state.get("position_episode") or {}
            health = health if isinstance(health, dict) else {}
            episode = episode if isinstance(episode, dict) else {}
            if (number(health.get("position_qty")) == position.get("qty") and
                    number(episode.get("average_entry_price")) == position.get("avg_entry_price")):
                mapping = {"state": "state", "score": "score", "recommended_action": "action", "as_of": "as_of",
                           "downside_score": "downside_score", "trend_score": "trend_score", "reward_risk_score": "reward_risk_score",
                           "stop_price": "stop_price", "stop_qty": "stop_qty", "data_fresh": "data_fresh", "data_complete": "data_complete", "reasons": "reasons"}
                selected = {"position_health_" + dst: health.get(src) for src, dst in mapping.items()}
                # Sanitize watcher values with the same field contract as reports.
                for key in POSITION_NUMBERS:
                    if key.startswith("position_health_"):
                        selected[key] = number(selected.get(key))
                for key in POSITION_TEXT:
                    if key.startswith("position_health_") and not isinstance(selected.get(key), str):
                        selected[key] = None
                selected["position_health_reasons"] = [r for r in (health.get("reasons") or []) if isinstance(r, str)]
                candidates.append((selected, "Watcher", state.get("position_health_last_evaluated_at")))
            valid = []
            for candidate, source, evaluated in candidates:
                bar, evaluated_at = timestamp(candidate.get("position_health_as_of")), timestamp(evaluated)
                if bar and evaluated_at and bar <= now and evaluated_at <= now:
                    valid.append((evaluated_at, bar, candidate, source))
            for key in list(position):
                if key.startswith("position_health_"):
                    del position[key]
            if valid:
                evaluated, bar, selected, source = max(valid, key=lambda item: (item[0], item[1]))
                position.update({k: v for k, v in selected.items() if k.startswith("position_health_")})
                position.update(position_health_source=source, position_health_evaluated_at=evaluated.isoformat())

    def snapshot(self):
        from traderbot.core_strategy_engine.position_health import assessment_bar_status
        with self.lock:
            data = copy.deepcopy(self.data)
        now = self.clock()
        for status in data["connection"].values():
            as_of = timestamp(status["as_of"])
            status["stale"] = bool(status["error"] or not as_of or (now - as_of).total_seconds() > 60)
        for p in data["positions"]:
            bar = timestamp(p.get("position_health_as_of"))
            status, bar_end = assessment_bar_status(bar, now,
                session_close=data["session_close"] if not data["connection"]["calendar"]["stale"] else None)
            if bar and not p.get("position_health_data_complete"):
                status = "Assessment incomplete"
            elif bar and not p.get("position_health_data_fresh"):
                status = "Assessment unavailable"
            p["position_health_freshness"] = status
            p["position_health_bar_end"] = bar_end
            p["position_health_current"] = bool(p.get("position_health_data_complete") and
                p.get("position_health_data_fresh") and status == "Latest completed bar")
        data["generated_at"] = now.isoformat()
        data["operations"] = self.operations.snapshot(p["symbol"] for p in data["positions"])
        return data
