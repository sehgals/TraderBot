"""Bounded, cached operational file reads; no process-running inference."""
import copy
import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

from traderbot.dashboard.server import number

UTC = dt.timezone.utc
TAIL_BYTES = 131072
JSON_BYTES = 2097152


def parsed(value):
    try:
        value = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def label(value, fallback="Unknown"):
    return value[:160] if isinstance(value, str) else fallback


class Operations:
    def __init__(self, root, clock=None):
        self.root = Path(root).resolve()
        self.clock = clock or (lambda: dt.datetime.now(UTC))
        self.files = {}
        self.lock = threading.Lock()
        self.cached = None
        self.held = None
        self.checked = float("-inf")

    def read(self, path, lines=False):
        """Reuse unchanged files and retain previous data on a torn write."""
        path = Path(path)
        previous = self.files.get(path, {})
        try:
            resolved = path.resolve()
            if not (resolved.is_relative_to(self.root / "runtime") or resolved == self.root / "config/watchers.json"):
                raise ValueError("Path outside allowed sources")
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
                if previous.get("fingerprint") == fingerprint:
                    return previous["data"], previous.get("issue")
                if lines:
                    start = max(0, stat.st_size - TAIL_BYTES)
                    stream.seek(start)
                    raw = stream.read(TAIL_BYTES)
                    chunks = raw.split(b"\n")
                    chunks = chunks[1:] if start else chunks
                    # Only newline-terminated records are committed log entries.
                    incomplete = bool(chunks[-1])
                    chunks = chunks[:-1]
                    data, malformed = [], False
                    for chunk in chunks:
                        if not chunk.strip():
                            continue
                        try:
                            item = json.loads(chunk)
                            if not isinstance(item, dict):
                                raise ValueError("Expected record")
                            data.append(item)
                        except (ValueError, UnicodeError):
                            malformed = True
                    data = data[-40:]
                    issue = "Partial or malformed records skipped" if incomplete or malformed else None
                    if not data and issue:
                        return previous.get("data", []), issue
                else:
                    if stat.st_size > JSON_BYTES:
                        raise ValueError("File too large")
                    data = json.loads(stream.read(JSON_BYTES + 1))
                    if not isinstance(data, dict):
                        raise ValueError("Expected object")
                    issue = None
            self.files[path] = {"data": data, "fingerprint": fingerprint, "issue": issue}
            return data, issue
        except (OSError, ValueError, UnicodeError):
            return previous.get("data", [] if lines else {}), "Source missing or unreadable; retained values may be outdated"

    def snapshot(self, held):
        held = frozenset(held)
        with self.lock:
            if self.cached is None or held != self.held or time.monotonic() - self.checked >= 5:
                self.cached = self.collect(held)
                self.held = held
                self.checked = time.monotonic()
            return copy.deepcopy(self.cached)

    def collect(self, held):
        now = self.clock()
        config, config_issue = self.read(self.root / "config/watchers.json")
        watchers = []
        for key in ("managed_watchers" if "managed_watchers" in config else "watchers", "new_watchers"):
            if isinstance(config.get(key), list):
                watchers.extend(w for w in config[key] if isinstance(w, dict))
        activity, statuses, candidates, alerts, issues = [], [], [], [], []
        if config_issue:
            issues.append({"source": "Watcher configuration", "message": config_issue})
        for watcher in watchers:
            symbol = label(watcher.get("symbol"))
            enabled = watcher.get("enabled", True) is not False
            log_path = watcher.get("log")
            records, log_issue = self.read(self.root / log_path, True) if isinstance(log_path, str) else ([], "Log path unavailable")
            valid = []
            for record in records:
                recorded = parsed(record.get("timestamp"))
                if not recorded or recorded > now:
                    log_issue = log_issue or "Invalid or future activity timestamps skipped"
                    continue
                result = record.get("result") if isinstance(record.get("result"), dict) else {}
                entry = {"symbol": symbol, "timestamp": recorded.isoformat(),
                         "status": label(result.get("status")), "failures": number(record.get("failures")),
                         "next_run_seconds": number(record.get("next_run_seconds"))}
                valid.append(entry)
            valid.sort(key=lambda row: row["timestamp"], reverse=True)
            latest = valid[0] if valid else None
            status, due = "Unknown", None
            if not enabled:
                status = "Disabled"
            elif log_issue or config_issue:
                status = "Source unavailable"
            elif latest:
                if latest["status"] == "market_closed_sleeping":
                    fallback = config.get("default_market_closed_poll_seconds", 900)
                elif latest["status"] == "temporary_error":
                    fallback = config.get("default_error_poll_seconds", 30)
                else:
                    fallback = config.get("default_poll_seconds", 30)
                delay = latest["next_run_seconds"]
                delay = delay if delay is not None and delay > 0 else number(fallback) or 30
                due = parsed(latest["timestamp"]) + dt.timedelta(seconds=min(delay, 86400))
                overdue = now > due + dt.timedelta(seconds=max(60, min(delay, 300)))
                status = "Overdue" if overdue else "Recent errors" if (latest["failures"] or 0) > 0 or latest["status"] == "temporary_error" else "Within schedule"
            statuses.append({"symbol": symbol, "status": status, "last_activity": latest["timestamp"] if latest else None,
                             "next_expected": due.isoformat() if due else None})
            activity.extend(valid[:3])
            if log_issue:
                issues.append({"source": symbol + " activity", "message": log_issue})
            for row in valid:
                if (row["failures"] or 0) > 0 or row["status"] == "temporary_error":
                    alerts.append({"symbol": symbol, "timestamp": row["timestamp"], "kind": "Watcher error", "detail": row["status"], "source": "Watcher log"})
            state_path = watcher.get("state")
            state, state_issue = self.read(self.root / state_path) if isinstance(state_path, str) else ({}, "State path unavailable")
            if state_issue:
                issues.append({"source": symbol + " state", "message": state_issue})
            plan = state.get("dynamic_entry_plan")
            if enabled and symbol not in held and isinstance(plan, dict) and plan:
                blockers = plan.get("hard_blockers") or plan.get("decision_reasons") or []
                threshold = number(plan.get("minimum_setup_score")) or 80
                score = number(plan.get("setup_score"))
                eligibility = state.get("flat_entry_eligibility") or {}
                decision_label = "Score below threshold" if score is None or score < threshold else "Score qualified - blocked" if blockers or eligibility.get("eligible") is False else "Score qualified - awaiting execution"
                metrics = plan.get("entry_filter_metrics") or {}
                diagnostics = {
                    "metrics": metrics, "reentry_policy": plan.get("reentry_policy"),
                    "ledger_cap": number(plan.get("ledger_cap")),
                    "eligibility_reasons": eligibility.get("reasons", []),
                    "submission_validation": state.get("entry_submission_validation") or plan.get("submission_validation"),
                }
                candidates.append({"decision_label": decision_label, "diagnostics": diagnostics, "minimum_score": threshold, "breakout_assessment": plan.get("breakout_assessment") if isinstance(plan.get("breakout_assessment"), dict) else None,"symbol": symbol, "score": number(plan.get("setup_score")),
                    "trend_assessment": plan.get("trend_assessment") if isinstance(plan.get("trend_assessment"), dict) else None,
                    "status": label(plan.get("status")), "model": label(plan.get("model_id") or plan.get("classified_model_id")),
                    "as_of": parsed(plan.get("last_bar_time")).isoformat() if parsed(plan.get("last_bar_time")) else None,
                    "source_status": "Retained / source unavailable" if state_issue or config_issue else "Recorded assessment",
                    "blockers": [label(x) for x in blockers if isinstance(x, str)][:20] if isinstance(blockers, list) else []})
        for name, source in (("position_health_alerts.jsonl", "Position health"), ("watcher_monitor_alerts.jsonl", "Watchdog")):
            records, issue = self.read(self.root / "runtime/logs" / name, True)
            if issue:
                issues.append({"source": source + " alerts", "message": issue})
            for row in records:
                recorded = parsed(row.get("timestamp"))
                if recorded and recorded <= now:
                    alerts.append({"symbol": label(row.get("symbol")), "timestamp": recorded.isoformat(),
                                   "kind": label(row.get("state"), "Watchdog notification"), "source": source,
                                   "detail": label(row.get("recommended_action"), "See local watchdog log for details")})
        activity.sort(key=lambda row: row["timestamp"], reverse=True)
        alerts.sort(key=lambda row: row["timestamp"], reverse=True)
        candidates.sort(key=lambda row: (-(row["score"] if row["score"] is not None else -1), row["symbol"]))
        counts = {status: sum(row["status"] == status for row in statuses) for status in
                  ("Within schedule", "Overdue", "Recent errors", "Unknown", "Source unavailable", "Disabled")}
        return {"as_of": now.isoformat(), "process_status": "Not verified",
                "watchers": statuses, "counts": counts, "activity": activity[:50], "alerts": alerts[:40],
                "candidates": candidates, "issues": issues,
                "note": "Activity status is based on log timestamps and expected poll intervals. It does not verify a running process. Alerts are historical events, not confirmed active incidents."}
