import json
import os
import threading
import time

import pytest

from traderbot.core_strategy_engine import engine
from traderbot.monitoring import reports


def test_save_json_round_trips_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state.json"
    engine.save_json(str(path), {"a": 1, "b": [1, 2]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2]}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_save_json_failure_keeps_previous_file_intact(tmp_path):
    path = tmp_path / "state.json"
    engine.save_json(str(path), {"good": True})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        engine.save_json(str(path), {"bad": Unserializable()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"good": True}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_concurrent_readers_never_see_partial_json(tmp_path):
    path = tmp_path / "state.json"
    engine.save_json(str(path), {"n": 0, "pad": "x" * 50000})
    stop = threading.Event()
    failures = []

    def reader():
        while not stop.is_set():
            try:
                with open(path, "r", encoding="utf-8") as file:
                    json.load(file)
            except PermissionError:
                continue  # transient Windows deny during replace; never a partial read
            except ValueError as error:
                failures.append(error)
            time.sleep(0.005)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for n in range(1, 60):
            engine.save_json(str(path), {"n": n, "pad": "x" * 50000})
    finally:
        stop.set()
        thread.join()
    assert failures == []


def test_load_state_json_returns_empty_for_nul_filled_file(tmp_path, capsys):
    path = tmp_path / "hubb_strategy_state.json"
    path.write_bytes(b"\x00" * 4096)
    assert reports.load_state_json(path) == {}
    assert "state_file_unreadable" in capsys.readouterr().err


def test_load_state_json_reads_valid_and_missing(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"x": 1}', encoding="utf-8")
    assert reports.load_state_json(path) == {"x": 1}
    assert reports.load_state_json(tmp_path / "missing.json") == {}


def test_strict_load_json_still_raises_on_corruption(tmp_path):
    path = tmp_path / "s.json"
    path.write_bytes(b"\x00" * 16)
    with pytest.raises(ValueError):
        engine.load_json(str(path), {})
