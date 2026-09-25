import ast
from collections import deque
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence


MAIN = Path(__file__).resolve().parents[2] / "research/lean/traderbot_helper/main.py"
tree = ast.parse(MAIN.read_text(encoding="utf-8"))
rule_names = {"breakout_ready", "entry_quantity", "partial_quantity", "ratcheted_runner_stop"}
rule_functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in rule_names]
namespace = {"math": math, "Sequence": Sequence}
exec(compile(ast.Module(body=rule_functions, type_ignores=[]), str(MAIN), "exec"), namespace)
rules = SimpleNamespace(**{name: namespace[name] for name in rule_names})


def test_breakout_uses_only_prior_bars_and_requires_volume():
    highs = [100.0] * 20
    volumes = [1000.0] * 20
    assert rules.breakout_ready(100.01, 1500, highs, volumes, 99, 2)
    assert not rules.breakout_ready(100.0, 1500, highs, volumes, 99, 2)
    assert not rules.breakout_ready(100.01, 1499, highs, volumes, 99, 2)
    assert not rules.breakout_ready(100.01, 1500, highs[:19], volumes, 99, 2)


def test_sizing_caps_risk_notional_and_keeps_runner():
    assert rules.entry_quantity(100000, 100, 5) == 100
    assert rules.entry_quantity(100000, 10, 50) == 20
    assert rules.entry_quantity(1000, 900, 5) == 0
    assert rules.partial_quantity(11, 11) == 5
    assert rules.partial_quantity(11, 3) == 2


def test_runner_stop_never_moves_down():
    assert rules.ratcheted_runner_stop(95, 90, 120, 4) == 110
    assert rules.ratcheted_runner_stop(110, 90, 115, 4) == 110


def test_hourly_consolidator_callback_accepts_sender_and_bar():
    algorithm = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    handler = next(node for node in algorithm.body if isinstance(node, ast.FunctionDef) and node.name == "on_hour_bar")
    callback_scope = {"TradeBar": object}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(MAIN), "exec"), callback_scope)
    state = SimpleNamespace(previous_hour_close=None, hourly_atr=None, hour_true_ranges=deque(maxlen=14))
    bar = SimpleNamespace(high=102, low=100, close=101)
    for _ in range(14):
        callback_scope["on_hour_bar"](state, object(), bar)
    assert state.hourly_atr == 2


def test_lean_algorithm_does_not_shadow_symbol_method():
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == "symbol"
        for node in ast.walk(tree)
    )
