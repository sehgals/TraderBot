import datetime
import unittest

from traderbot.core_strategy_engine.engine import (
    adaptive_ladder_limit,
    adaptive_ladder_quantity,
    adverse_reduction_details,
    apply_ladder_risk_caps,
    apply_order_risk_caps,
    cash_available_share_count,
    cash_protected_quantity,
    cancel_tracked_legacy_ladder_orders,
    calculate_indicators,
    completed_market_bars,
    dynamic_entry_plan,
    dynamic_entry_quantity,
    effective_managed_stop_price,
    ensure_catastrophic_stop,
    ensure_position_episode,
    evaluate_flat_entry_eligibility,
    initial_floor_price,
    initial_entry_target,
    observe_ladder_opportunities,
    position_health_config,
    position_health_market_context,
    reconcile_flat_position_state,
    resolve_adverse_reduction_order,
    run_once,
    submit_confirmed_health_action,
    submit_position_health_add,
    submit_adverse_reduction,
    structural_stop_price,
    trail_below_current_percent,
    update_dynamic_pending_order,
    update_stop_order,
)


class FakeClient:
    def __init__(self, position=None, orders=None, account=None):
        self._position = position
        self.orders = list(orders or [])
        self._account = account or {"equity": "100000", "cash": "100000", "buying_power": "100000"}
        self.canceled = []
        self.submitted = []
        self.replaced = []

    def position(self, symbol):
        return self._position

    def order(self, order_id):
        for order in self.orders:
            if order["id"] == order_id:
                return order
        raise AssertionError(f"unknown order {order_id}")

    def open_stop_orders(self, symbol):
        return [
            order
            for order in self.orders
            if order.get("symbol") == symbol
            and order.get("side") == "sell"
            and order.get("type") == "stop"
            and order.get("status") in ("new", "accepted", "pending_new", "partially_filled")
        ]

    def open_buy_orders(self, symbol):
        return [
            order
            for order in self.orders
            if order.get("symbol") == symbol
            and order.get("side") == "buy"
            and order.get("status") in ("new", "accepted", "pending_new", "partially_filled")
        ]

    def cancel_order(self, order_id):
        self.canceled.append(order_id)

    def submit_order(self, payload):
        order = {"id": f"new-{len(self.submitted) + 1}", **payload}
        self.submitted.append(order)
        return order

    def replace_order(self, order_id, payload):
        self.replaced.append((order_id, payload))
        order = {"id": order_id, **payload}
        return order

    def account(self):
        return self._account

    def latest_trade_price(self, symbol):
        return float(self._position["current_price"])


class FakeHealthClient(FakeClient):
    def stock_bars(self, symbol, start, end, timeframe):
        now = datetime.datetime.now(datetime.timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        bars = []
        for index in range(80):
            close = 95 + index * (0.10 if symbol == "WAT" else 0.05)
            bars.append(
                {
                    "t": (now - datetime.timedelta(hours=79 - index)).isoformat(),
                    "o": close - 0.1,
                    "h": close + 0.2,
                    "l": close - 0.2,
                    "c": close,
                    "v": 1000 + index,
                }
            )
        return bars


class RiskControlTests(unittest.TestCase):
    def test_ladder_trigger_is_observation_only(self):
        observations, _detail = observe_ladder_opportunities(
            {
                "ladder_buy_quantity": 25,
                "ladder_drop_steps_percent": [2],
                "max_ladder_count": 1,
            },
            {"filled_ladder_steps": []},
            100,
            97,
            None,
            {"state": "Healthy", "score": 90},
        )

        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0]["observation_only"])
        self.assertEqual(
            observations[0]["status"], "position_health_authority_required"
        )
        self.assertEqual(observations[0]["proposed_qty"], 25)

    def test_only_tracked_legacy_ladder_order_is_canceled(self):
        client = FakeClient(
            orders=[
                {
                    "id": "ladder-1",
                    "symbol": "WAT",
                    "side": "buy",
                    "type": "market",
                    "status": "new",
                },
                {
                    "id": "manual-1",
                    "symbol": "WAT",
                    "side": "buy",
                    "type": "limit",
                    "status": "new",
                },
            ]
        )
        state = {"ladder_order_ids": {"percent:2": "ladder-1"}}

        canceled = cancel_tracked_legacy_ladder_orders(
            client, {"symbol": "WAT"}, state
        )

        self.assertEqual(canceled, ["ladder-1"])
        self.assertEqual(client.canceled, ["ladder-1"])
        self.assertNotIn("manual-1", client.canceled)
        self.assertEqual(state["ladder_order_ids"], {})
    def test_pending_entry_is_canceled_instead_of_switching_models(self):
        client = FakeClient(
            orders=[
                {
                    "id": "entry-1",
                    "symbol": "WAT",
                    "side": "buy",
                    "type": "limit",
                    "status": "new",
                    "qty": "5",
                    "limit_price": "100",
                }
            ]
        )
        state = {
            "current_entry_order_id": "entry-1",
            "reentry_order_id": "entry-1",
            "pending_entry_model_id": "pullback_reclaim",
        }
        plan = {
            "status": "active_signal",
            "model_id": "breakout_continuation",
            "model_version": 1,
            "mode": "dynamic_breakout_continuation",
            "last_bar_time": "2026-08-21T19:55:00Z",
            "limit_price": 101,
        }

        result = update_dynamic_pending_order(
            client, {"symbol": "WAT"}, state, "entry-1", plan
        )

        self.assertEqual(result["status"], "dynamic_entry_order_canceled_model_switch")
        self.assertEqual(client.canceled, ["entry-1"])
        self.assertNotIn("current_entry_order_id", state)
        self.assertNotIn("pending_entry_model_id", state)

    def test_pending_entry_is_canceled_when_originating_signal_is_inactive(self):
        client = FakeClient(
            orders=[
                {
                    "id": "entry-1",
                    "symbol": "WAT",
                    "side": "buy",
                    "type": "limit",
                    "status": "new",
                    "qty": "5",
                    "limit_price": "100",
                }
            ]
        )
        state = {
            "current_entry_order_id": "entry-1",
            "reentry_order_id": "entry-1",
            "pending_entry_model_id": "pullback_reclaim",
        }
        plan = {
            "status": "watch",
            "model_id": None,
            "classified_model_id": "pullback_reclaim",
        }

        result = update_dynamic_pending_order(
            client, {"symbol": "WAT"}, state, "entry-1", plan
        )

        self.assertEqual(result["status"], "dynamic_entry_order_canceled_signal_inactive")
        self.assertEqual(client.canceled, ["entry-1"])
        self.assertNotIn("current_entry_order_id", state)
    def test_rvol_compares_matched_time_dollar_volume_across_twenty_sessions(self):
        raw = []
        start = datetime.datetime(2026, 1, 5, 20, 55, tzinfo=datetime.timezone.utc)
        for day in range(21):
            timestamp = start + datetime.timedelta(days=day)
            raw.append(
                {
                    "t": (timestamp - datetime.timedelta(minutes=5)).isoformat(),
                    "o": 100,
                    "h": 100,
                    "l": 100,
                    "c": 100,
                    "v": 100_000,
                }
            )
            raw.append(
                {
                    "t": timestamp.isoformat(),
                    "o": 100,
                    "h": 100,
                    "l": 100,
                    "c": 100,
                    "v": 2_000 if day == 20 else 1_000,
                }
            )

        latest = calculate_indicators(raw)[-1]

        self.assertAlmostEqual(latest["volume_ratio"], 2.0)
        self.assertEqual(latest["rvol_sample_size"], 20)
        self.assertEqual(latest["matched_average_dollar_volume"], 100_000)
        self.assertEqual(latest["dollar_volume_sample_size"], 20)
        self.assertEqual(
            latest["rvol_method"],
            "matched_eastern_time_20_session_dollar_volume",
        )

    def test_session_gap_uses_prior_eastern_session_close(self):
        raw = [
            {"t": "2026-08-20T19:55:00Z", "o": 99, "h": 101, "l": 99, "c": 100, "v": 10},
            {"t": "2026-08-21T13:30:00Z", "o": 105, "h": 106, "l": 104, "c": 105, "v": 10},
            {"t": "2026-08-21T13:35:00Z", "o": 105, "h": 105, "l": 104, "c": 104, "v": 10},
        ]

        bars = calculate_indicators(raw)

        self.assertIsNone(bars[0]["session_gap_percent"])
        self.assertEqual(bars[1]["session_gap_percent"], 5.0)
        self.assertEqual(bars[2]["session_gap_percent"], 5.0)

    def test_structural_stop_drives_risk_based_quantity_and_entry_target(self):
        stop = structural_stop_price({}, 100, 2, 90)
        plan = {"limit_price": 100, "stop_price": stop, "target_price": 110}

        self.assertEqual(stop, 94)
        self.assertEqual(dynamic_entry_quantity({}, plan, equity=100_000), 83)
        self.assertEqual(dynamic_entry_quantity({}, plan, equity=1_000), 0)
        self.assertEqual(
            initial_entry_target({}, {"dynamic_entry_plan": plan}, 100),
            (110, "entry_structural_target"),
        )

    def test_market_and_sector_regimes_are_hard_entry_gates(self):
        start = datetime.datetime(2026, 1, 5, 14, 30, tzinfo=datetime.timezone.utc)
        stock_bars = []
        for index in range(55):
            is_latest = index == 54
            stock_bars.append(
                {
                    "t": start + datetime.timedelta(minutes=5 * index),
                    "o": 100,
                    "h": 102 if is_latest else 101,
                    "l": 99,
                    "c": 102 if is_latest else 100,
                    "v": 1_000,
                    "ema9": 101 if is_latest else 99.5,
                    "ema21": 100 if is_latest else 98 + index * 0.02,
                    "ema50": 98,
                    "atr14": 1,
                    "vwap": 99,
                    "volume_ratio": 2 if is_latest else 1,
                }
            )

        def regime_bars(favorable):
            result = []
            for index in range(6):
                result.append(
                    {
                        "t": stock_bars[-6 + index]["t"],
                        "c": 102 if favorable else 99,
                        "ema21": 100 + index * 0.1,
                        "vwap": 100,
                    }
                )
            return result

        market_blocked = dynamic_entry_plan(
            "TEST",
            stock_bars,
            regime_bars(False),
            sector_bars=regime_bars(True),
        )
        sector_blocked = dynamic_entry_plan(
            "TEST",
            stock_bars,
            regime_bars(True),
            sector_bars=regime_bars(False),
        )

        self.assertEqual(market_blocked["status"], "watch")
        self.assertIn("market_ok", market_blocked["blockers"])
        self.assertEqual(sector_blocked["status"], "watch")
        self.assertIn("sector_ok", sector_blocked["blockers"])

    def test_completed_market_bars_excludes_in_progress_interval(self):
        raw = [
            {"t": "2026-08-21T19:50:00Z"},
            {"t": "2026-08-21T19:55:00Z"},
        ]

        result = completed_market_bars(
            raw,
            "5Min",
            now=datetime.datetime(2026, 8, 21, 19, 58, tzinfo=datetime.timezone.utc),
        )

        self.assertEqual(result, raw[:1])

    def test_position_health_uses_symbol_sector_benchmark(self):
        client = FakeHealthClient(position={})
        requested_symbols = []
        original_stock_bars = client.stock_bars

        def recording_stock_bars(symbol, start, end, timeframe):
            requested_symbols.append(symbol)
            return original_stock_bars(symbol, start, end, timeframe)

        client.stock_bars = recording_stock_bars
        context = position_health_market_context(
            client,
            {
                "symbol": "WAT",
                "position_health": {
                    "benchmark_symbol": "SPY",
                    "benchmark_by_symbol": {"WAT": "XLV"},
                },
            },
        )

        self.assertEqual(requested_symbols, ["WAT", "XLV"])
        self.assertEqual(context["benchmark_symbol"], "XLV")

    def test_position_health_add_respects_cash_reserve_and_twenty_percent_cap(self):
        position = {
            "symbol": "WAT",
            "qty": "100",
            "avg_entry_price": "100",
            "current_price": "110",
            "market_value": "11000",
        }
        client = FakeClient(
            position=position,
            account={"equity": "100000", "cash": "30000", "buying_power": "100000"},
        )
        state = {
            "position_episode": {
                "episode_id": "episode-1",
                "initial_qty": 100,
                "add_count": 0,
                "entry_setup_score": 92,
                "entry_setup_status": "active_signal",
            }
        }
        health = {
            "score": 90,
            "data_fresh": True,
            "remaining_r": 2.0,
            "components": {"trend_checks": {
                "price_above_ema21": True,
                "price_above_ema50": True,
                "ema21_slope_positive": True,
                "market_regime_favorable": True,
            }},
        }
        config = {
            "symbol": "WAT",
            "min_cash_balance_percent": 20,
            "position_health": {
                "shadow_mode": False,
                "additions_enabled": True,
                "max_symbol_notional_percent": 20,
            },
        }

        result = submit_position_health_add(
            client, config, state, position, health, stop_price=100
        )

        self.assertEqual(result["status"], "position_health_add_order_submitted")
        self.assertEqual(result["qty"], 50)
        self.assertEqual(client.submitted[0]["side"], "buy")
        self.assertEqual(state["position_episode"]["add_count"], 1)

    def test_position_health_add_is_blocked_when_cash_reserve_would_be_used(self):
        position = {
            "symbol": "WAT", "qty": "100", "avg_entry_price": "100",
            "current_price": "110", "market_value": "11000",
        }
        client = FakeClient(
            position=position,
            account={"equity": "100000", "cash": "20000", "buying_power": "100000"},
        )
        state = {"position_episode": {
            "episode_id": "episode-1", "initial_qty": 100, "add_count": 0,
            "entry_setup_score": 92, "entry_setup_status": "active_signal",
        }}
        health = {
            "score": 90, "data_fresh": True, "remaining_r": 2.0,
            "components": {"trend_checks": {
                "price_above_ema21": True, "price_above_ema50": True,
                "ema21_slope_positive": True, "market_regime_favorable": True,
            }},
        }
        config = {"symbol": "WAT", "min_cash_balance_percent": 20,
                  "position_health": {"shadow_mode": False, "additions_enabled": True,
                                      "max_symbol_notional_percent": 20}}

        result = submit_position_health_add(
            client, config, state, position, health, stop_price=100
        )

        self.assertIsNone(result)
        self.assertEqual(client.submitted, [])
        self.assertIn("cash_reserve_blocked", state["position_health_add_eligibility"]["reasons"])

    def test_position_health_config_uses_alpaca_hourly_timeframe(self):
        self.assertEqual(position_health_config({})["timeframe"], "1Hour")
        self.assertEqual(
            position_health_config(
                {"position_health": {"timeframe": "60Min"}}
            )["timeframe"],
            "1Hour",
        )

    def test_managed_position_persists_fresh_position_health_snapshot(self):
        client = FakeHealthClient(
            position={
                "symbol": "WAT",
                "qty": "10",
                "avg_entry_price": "100",
                "current_price": "105",
            }
        )
        config = {
            "symbol": "WAT",
            "entry_quantity": 10,
            "initial_stop_loss_percent": 20,
            "trail_trigger_step_percent": 5,
            "trail_stop_below_current_percent": 2.5,
            "ladder_buy_quantity": 0,
            "ladder_drop_steps_percent": [],
            "position_health": {
                "enabled": True,
                "shadow_mode": True,
                "timeframe": "1Hour",
                "lookback_days": 45,
                "refresh_seconds": 3300,
            },
        }
        state = {}

        result = run_once(client, config, state, clock={"is_open": True})

        self.assertEqual(result["status"], "managed")
        self.assertEqual(result["position_health"]["state"], "Healthy")
        self.assertTrue(result["position_health"]["data_fresh"])
        self.assertEqual(state["position_episode"]["symbol"], "WAT")
        self.assertEqual(state["position_health"]["model_version"], "health-v1")

    def test_first_fill_creates_position_episode_and_preserves_setup(self):
        position = {
            "symbol": "WAT",
            "qty": "4",
            "avg_entry_price": "400",
            "current_price": "401",
        }
        state = {
            "current_entry_order_id": "entry-1",
            "dynamic_entry_plan": {
                "status": "active_signal",
                "mode": "dynamic_breakout_continuation",
                "last_bar_time": "2026-08-11T15:55:00Z",
                "atr14": 4,
                "breakout_limit": 400,
                "blockers": ["no_chase"],
            },
        }

        episode = ensure_position_episode(
            {"symbol": "WAT"},
            state,
            position,
            entry_order={"id": "entry-1", "status": "partially_filled"},
        )

        self.assertEqual(episode["state"], "OPEN_PARTIAL")
        self.assertEqual(episode["entry_setup_score"], 92)
        self.assertEqual(episode["original_target_price"], 408)
        self.assertEqual(episode["origin_model_id"], "breakout_continuation")
        self.assertEqual(episode["origin_model_version"], 0)
        self.assertEqual(state["managed_entry_order_id"], "entry-1")

    def test_position_episode_model_contract_is_immutable_after_first_fill(self):
        position = {
            "symbol": "WAT",
            "qty": "4",
            "avg_entry_price": "100",
            "current_price": "101",
        }
        state = {
            "current_entry_order_id": "entry-1",
            "pending_entry_model_id": "breakout_continuation",
            "pending_entry_model_version": 1,
            "dynamic_entry_plan": {
                "status": "active_signal",
                "mode": "dynamic_breakout_continuation",
                "model_id": "breakout_continuation",
                "model_version": 1,
                "setup_score": 100,
                "model_checks": {"breakout_now": True},
                "last_bar_time": "2026-08-11T15:55:00Z",
                "limit_price": 100,
                "stop_price": 96,
                "target_price": 108,
                "risk_per_share": 4,
                "expected_reward_risk": 2,
            },
        }

        episode = ensure_position_episode(
            {"symbol": "WAT"},
            state,
            position,
            entry_order={"id": "entry-1", "status": "partially_filled"},
        )
        original_hash = episode["entry_candidate_hash"]
        original_snapshot = dict(episode["entry_candidate_snapshot"])
        state["dynamic_entry_plan"] = {
            "status": "active_signal",
            "mode": "dynamic_pullback_reclaim",
            "model_id": "pullback_reclaim",
            "model_version": 1,
            "setup_score": 77,
            "target_price": 105,
        }
        position.update({"qty": "5", "avg_entry_price": "100.50"})

        refreshed = ensure_position_episode({"symbol": "WAT"}, state, position)

        self.assertEqual(refreshed["origin_model_id"], "breakout_continuation")
        self.assertEqual(refreshed["origin_model_version"], 1)
        self.assertEqual(refreshed["entry_setup_score"], 100)
        self.assertEqual(refreshed["entry_candidate_hash"], original_hash)
        self.assertEqual(refreshed["entry_candidate_snapshot"], original_snapshot)
        self.assertEqual(refreshed["current_qty"], 5)
        self.assertEqual(refreshed["average_entry_price"], 100.5)

    def test_health_action_is_inert_in_shadow_mode(self):
        client = FakeClient(
            position={
                "symbol": "WAT",
                "qty": "10",
                "avg_entry_price": "400",
                "current_price": "390",
            }
        )
        state = {
            "position_health_confirmation": {"action": "reduce", "count": 2},
            "position_episode": {"episode_id": "episode-1"},
        }

        result = submit_confirmed_health_action(
            client,
            {"symbol": "WAT", "position_health": {"shadow_mode": True}},
            state,
            client._position,
            {"recommended_action": "reduce", "score": 30, "state": "At Risk"},
        )

        self.assertIsNone(result)
        self.assertEqual(client.submitted, [])

    def test_confirmed_health_reduction_submits_once_when_enabled(self):
        client = FakeClient(
            position={
                "symbol": "WAT",
                "qty": "10",
                "avg_entry_price": "400",
                "current_price": "390",
            },
            orders=[
                {
                    "id": "stop-1",
                    "symbol": "WAT",
                    "side": "sell",
                    "type": "stop",
                    "status": "new",
                    "qty": "10",
                    "stop_price": "368",
                }
            ],
        )
        state = {
            "active_stop_order_id": "stop-1",
            "position_health_confirmation": {"action": "reduce", "count": 2},
            "position_episode": {"episode_id": "episode-1", "state_version": 1},
        }

        result = submit_confirmed_health_action(
            client,
            {
                "symbol": "WAT",
                "position_health": {
                    "shadow_mode": False,
                    "reduction_confirmation_bars": 2,
                },
            },
            state,
            client._position,
            {"recommended_action": "reduce", "score": 30, "state": "At Risk"},
        )

        self.assertEqual(result["status"], "position_health_reduce_order_submitted")
        self.assertEqual(result["qty"], 5)
        self.assertEqual(client.canceled, ["stop-1"])
        self.assertEqual(client.submitted[0]["side"], "sell")
        self.assertEqual(client.submitted[0]["client_order_id"].split("-")[1], "health")

    def test_adverse_reduction_sells_half_once_at_six_percent_loss(self):
        position = {
            "symbol": "WAT",
            "qty": "11",
            "avg_entry_price": "400",
            "current_price": "375",
        }
        client = FakeClient(
            position=position,
            orders=[
                {
                    "id": "protective-stop",
                    "symbol": "WAT",
                    "side": "sell",
                    "type": "stop",
                    "status": "new",
                    "qty": "11",
                    "stop_price": "368",
                }
            ],
        )
        state = {
            "active_stop_order_id": "protective-stop",
            "active_stop_price": "368.00",
            "active_stop_qty": 11,
        }

        result = submit_adverse_reduction(client, {"symbol": "WAT"}, position, 375, state)

        self.assertEqual(result["status"], "adverse_reduction_order_submitted")
        self.assertEqual(result["trigger_price"], 376)
        self.assertEqual(result["qty"], 6)
        self.assertEqual(client.canceled, ["protective-stop"])
        self.assertEqual(client.submitted[0]["side"], "sell")
        self.assertEqual(client.submitted[0]["type"], "market")
        self.assertEqual(client.submitted[0]["qty"], "6")
        self.assertNotIn("active_stop_order_id", state)

    def test_adverse_reduction_does_not_trigger_above_threshold(self):
        position = {
            "symbol": "WAT",
            "qty": "10",
            "avg_entry_price": "400",
            "current_price": "377",
        }
        client = FakeClient(position=position)

        result = submit_adverse_reduction(client, {"symbol": "WAT"}, position, 377, {})

        self.assertIsNone(result)
        self.assertEqual(client.submitted, [])

    def test_adverse_reduction_pending_order_is_not_duplicated(self):
        client = FakeClient(
            orders=[
                {
                    "id": "reduce-1",
                    "symbol": "WAT",
                    "side": "sell",
                    "type": "market",
                    "status": "accepted",
                    "qty": "5",
                }
            ]
        )
        state = {"adverse_reduction_order_id": "reduce-1"}

        result = resolve_adverse_reduction_order(client, state)

        self.assertEqual(result["status"], "adverse_reduction_order_pending")
        self.assertEqual(state["adverse_reduction_order_id"], "reduce-1")

    def test_filled_adverse_reduction_sets_one_time_lockout(self):
        client = FakeClient(
            orders=[
                {
                    "id": "reduce-1",
                    "symbol": "WAT",
                    "side": "sell",
                    "type": "market",
                    "status": "filled",
                    "qty": "5",
                    "filled_qty": "5",
                    "filled_avg_price": "375",
                }
            ]
        )
        state = {"adverse_reduction_order_id": "reduce-1"}

        result = resolve_adverse_reduction_order(client, state)

        self.assertEqual(result["status"], "adverse_reduction_filled")
        self.assertTrue(state["adverse_reduction_completed"])
        self.assertNotIn("adverse_reduction_order_id", state)

    def test_adverse_reduction_configuration_validation(self):
        with self.assertRaises(ValueError):
            adverse_reduction_details(
                {"adverse_reduction_trigger_percent": 0}, 100, 10
            )
        with self.assertRaises(ValueError):
            adverse_reduction_details(
                {"adverse_reduction_fraction": 1.1}, 100, 10
            )

    def test_partial_entry_fill_gets_immediate_broker_stop(self):
        client = FakeClient(
            position={
                "symbol": "WAT",
                "qty": "4",
                "avg_entry_price": "400",
                "current_price": "399",
            },
            orders=[
                {
                    "id": "entry-1",
                    "symbol": "WAT",
                    "side": "buy",
                    "type": "limit",
                    "status": "partially_filled",
                    "qty": "12",
                    "filled_qty": "4",
                    "filled_avg_price": "400",
                }
            ],
        )
        config = {"symbol": "WAT", "catastrophic_stop_loss_percent": 8}
        state = {"current_entry_order_id": "entry-1"}

        result = run_once(client, config, state, clock={"is_open": True})

        self.assertEqual(result["status"], "waiting_for_entry_fill")
        self.assertEqual(result["catastrophic_stop"]["stop_price"], "368.00")
        self.assertEqual(client.submitted[0]["side"], "sell")
        self.assertEqual(client.submitted[0]["type"], "stop")
        self.assertEqual(client.submitted[0]["qty"], "4")

    def test_catastrophic_stop_adopts_existing_tighter_stop(self):
        client = FakeClient(
            position={
                "symbol": "WAT",
                "qty": "4",
                "avg_entry_price": "400",
                "current_price": "410",
            },
            orders=[
                {
                    "id": "existing-stop",
                    "symbol": "WAT",
                    "side": "sell",
                    "type": "stop",
                    "status": "new",
                    "qty": "4",
                    "stop_price": "390.00",
                }
            ],
        )
        state = {}

        result = ensure_catastrophic_stop(client, {"symbol": "WAT"}, client._position, state)

        self.assertFalse(result["created"])
        self.assertEqual(result["order_id"], "existing-stop")
        self.assertEqual(client.submitted, [])
        self.assertEqual(state["active_stop_price"], "390.00")

    def test_catastrophic_stop_uses_current_price_fallback_after_gap(self):
        client = FakeClient(
            position={
                "symbol": "WAT",
                "qty": "4",
                "avg_entry_price": "400",
                "current_price": "350",
            }
        )

        result = ensure_catastrophic_stop(client, {"symbol": "WAT"}, client._position, {})

        self.assertEqual(result["stop_price"], "341.25")
        self.assertEqual(client.submitted[0]["stop_price"], "341.25")

    def test_restart_while_market_closed_restores_missing_stop(self):
        client = FakeClient(
            position={
                "symbol": "WAT",
                "qty": "4",
                "avg_entry_price": "400",
                "current_price": "399",
            }
        )
        config = {"symbol": "WAT", "catastrophic_stop_loss_percent": 8}

        result = run_once(
            client,
            config,
            {},
            clock={"is_open": False, "timestamp": "now", "next_open": "later"},
        )

        self.assertEqual(result["status"], "market_closed_sleeping")
        self.assertEqual(result["catastrophic_stop"]["stop_price"], "368.00")
        self.assertEqual(client.submitted[0]["type"], "stop")

    def test_flat_position_cancels_open_stops(self):
        client = FakeClient(
            position=None,
            orders=[
                {
                    "id": "stop-1",
                    "symbol": "SPY",
                    "side": "sell",
                    "type": "stop",
                    "status": "new",
                    "qty": "100",
                    "stop_price": "590",
                }
            ],
        )
        state = {"active_stop_order_id": "stop-1", "active_stop_price": "590.00", "active_stop_qty": 100}

        result = reconcile_flat_position_state(client, "SPY", state)

        self.assertEqual(client.canceled, ["stop-1"])
        self.assertEqual(result["canceled_stop_order_ids"], ["stop-1"])
        self.assertNotIn("active_stop_order_id", state)

    def test_flat_reconciliation_archives_closed_position_episode(self):
        client = FakeClient(position=None)
        state = {
            "position_episode": {
                "episode_id": "episode-1",
                "symbol": "WAT",
                "state": "OPEN",
            },
            "position_health": {"score": 42, "state": "At Risk"},
        }

        reconcile_flat_position_state(client, "WAT", state)

        self.assertNotIn("position_episode", state)
        self.assertEqual(len(state["closed_position_episodes"]), 1)
        archived = state["closed_position_episodes"][0]
        self.assertEqual(archived["episode_id"], "episode-1")
        self.assertEqual(archived["state"], "CLOSED")
        self.assertEqual(archived["final_health"]["score"], 42)

    def test_flat_reconciliation_recovers_manual_exit_from_broker_fills(self):
        client = FakeClient(position=None)
        client.fills = lambda after: [
            {
                "symbol": "WAT",
                "side": "sell",
                "qty": "4",
                "price": "412.50",
                "order_id": "manual-sell",
                "transaction_time": "2026-08-18T14:05:00Z",
            }
        ]
        state = {
            "entry_fill_price": 400,
            "active_stop_price": "368.00",
            "last_position_seen_at": "2026-08-18T14:00:00Z",
        }

        result = reconcile_flat_position_state(client, "WAT", state)

        self.assertEqual(result["last_exit_order_id"], "manual-sell")
        self.assertEqual(state["last_exit_price"], 412.50)
        self.assertEqual(state["last_exit_entry_price"], 400)
        self.assertEqual(state["last_exit_source"], "broker_fill_reconstruction")
        self.assertNotIn("exit_record_missing", state)

    def test_flat_reconciliation_marks_missing_exit_without_guessing(self):
        client = FakeClient(position=None)
        client.fills = lambda after: []
        state = {
            "entry_fill_price": 400,
            "active_stop_price": "368.00",
            "last_position_seen_at": "2026-08-18T14:00:00Z",
        }

        reconcile_flat_position_state(client, "WAT", state)

        self.assertTrue(state["exit_record_missing"])
        self.assertNotIn("last_exit_price", state)

    def test_flat_reconciliation_combines_multiple_closing_sell_orders(self):
        client = FakeClient(position=None)
        client.fills = lambda after: [
            {
                "symbol": "WAT", "side": "sell", "qty": "2", "price": "410",
                "order_id": "sell-1", "transaction_time": "2026-08-18T14:04:00Z",
            },
            {
                "symbol": "WAT", "side": "sell", "qty": "2", "price": "414",
                "order_id": "sell-2", "transaction_time": "2026-08-18T14:05:00Z",
            },
        ]
        state = {
            "entry_fill_price": 400,
            "active_stop_price": "368.00",
            "last_position_seen_at": "2026-08-18T14:00:00Z",
            "last_position_qty": 4,
        }

        reconcile_flat_position_state(client, "WAT", state)

        self.assertEqual(state["last_exit_price"], 412)
        self.assertEqual(state["last_exit_qty"], 4)
        self.assertEqual(state["last_exit_order_ids"], ["sell-2", "sell-1"])

    def test_managed_flat_symbol_without_exit_routes_to_new_entry(self):
        client = FakeHealthClient(position=None)
        result = run_once(
            client,
            {
                "symbol": "WAT",
                "dynamic_entry_enabled": False,
                "reentry_enabled": True,
                "dynamic_reentry_enabled": True,
            },
            {},
            clock={"is_open": True, "timestamp": "2026-08-18T14:00:00Z"},
        )

        self.assertEqual(result["status"], "dynamic_entry_waiting_for_signal")
        self.assertEqual(result["eligibility"]["mode"], "new_entry")
        self.assertNotIn("new_entry_disabled", result["eligibility"]["reasons"])
        self.assertNotIn("exit_record_missing", result["eligibility"]["reasons"])

    def test_flat_entry_eligibility_rejects_stale_plan(self):
        eligibility = evaluate_flat_entry_eligibility(
            {"dynamic_entry_enabled": True, "dynamic_timeframe": "5Min"},
            {},
            {"status": "active_signal", "last_bar_time": "2026-08-18T13:00:00Z"},
            now=datetime.datetime(2026, 8, 18, 14, 0, tzinfo=datetime.timezone.utc),
            mode="new_entry",
        )

        self.assertFalse(eligibility["eligible"])
        self.assertIn("stale_entry_plan", eligibility["reasons"])

    def test_flat_entry_eligibility_accepts_closing_plan_after_hours(self):
        eligibility = evaluate_flat_entry_eligibility(
            {"dynamic_entry_enabled": True, "dynamic_timeframe": "5Min"},
            {},
            {"status": "active_signal", "last_bar_time": "2026-08-21T19:55:00Z"},
            now=datetime.datetime(2026, 8, 21, 20, 18, tzinfo=datetime.timezone.utc),
            mode="new_entry",
        )

        self.assertTrue(eligibility["eligible"])
        self.assertNotIn("stale_entry_plan", eligibility["reasons"])
        self.assertEqual(eligibility["plan_age_seconds"], 23 * 60)

    def test_flat_entry_eligibility_rejects_old_same_day_plan_after_hours(self):
        eligibility = evaluate_flat_entry_eligibility(
            {"dynamic_entry_enabled": True, "dynamic_timeframe": "5Min"},
            {},
            {"status": "active_signal", "last_bar_time": "2026-08-21T17:00:00Z"},
            now=datetime.datetime(2026, 8, 21, 20, 18, tzinfo=datetime.timezone.utc),
            mode="new_entry",
        )

        self.assertFalse(eligibility["eligible"])
        self.assertIn("stale_entry_plan", eligibility["reasons"])

    def test_managed_reentry_observe_only_never_becomes_order_eligible(self):
        eligibility = evaluate_flat_entry_eligibility(
            {
                "reentry_enabled": True,
                "dynamic_reentry_enabled": True,
                "reentry_observe_only": True,
            },
            {
                "last_exit_price": 100,
                "last_exit_at": "2026-08-18T13:00:00Z",
            },
            {"status": "active_signal", "last_bar_time": "2026-08-18T13:55:00Z"},
            now=datetime.datetime(2026, 8, 18, 14, 0, tzinfo=datetime.timezone.utc),
            mode="reentry",
        )

        self.assertFalse(eligibility["eligible"])
        self.assertEqual(eligibility["reasons"], ["reentry_observe_only"])

    def test_flat_reconciliation_preserves_untracked_open_buy_order(self):
        client = FakeClient(
            position=None,
            orders=[
                {
                    "id": "overnight-buy",
                    "symbol": "WAT",
                    "side": "buy",
                    "type": "limit",
                    "status": "accepted",
                    "qty": "6",
                    "limit_price": "398.96",
                }
            ],
        )
        state = {}

        result = reconcile_flat_position_state(client, "WAT", state)

        self.assertEqual(result["open_buy_order_ids"], ["overnight-buy"])
        self.assertEqual(state["current_entry_order_id"], "overnight-buy")
        self.assertEqual(state["reentry_order_id"], "overnight-buy")

    def test_update_stop_cleans_duplicate_when_state_matches(self):
        client = FakeClient(
            position={"qty": "100"},
            orders=[
                {
                    "id": "active",
                    "symbol": "SPY",
                    "side": "sell",
                    "type": "stop",
                    "status": "new",
                    "qty": "100",
                    "stop_price": "590.00",
                },
                {
                    "id": "duplicate",
                    "symbol": "SPY",
                    "side": "sell",
                    "type": "stop",
                    "status": "new",
                    "qty": "100",
                    "stop_price": "580.00",
                },
            ],
        )
        state = {"active_stop_order_id": "active", "active_stop_price": "590.00", "active_stop_qty": 100}

        self.assertIsNone(update_stop_order(client, "SPY", 100, 590, state))
        self.assertEqual(client.canceled, ["duplicate"])

    def test_saved_filled_entry_order_loads_live_quantity_before_managing_position(self):
        client = FakeClient(
            position={
                "symbol": "GFS",
                "qty": "10",
                "avg_entry_price": "66.82",
                "current_price": "46.00",
            },
            orders=[
                {
                    "id": "entry-1",
                    "symbol": "GFS",
                    "side": "buy",
                    "type": "limit",
                    "status": "filled",
                    "qty": "74",
                    "filled_avg_price": "66.82",
                }
            ],
        )
        config = {
            "symbol": "GFS",
            "entry_quantity": 1,
            "initial_stop_loss_percent": 20,
            "adverse_reduction_trigger_percent": 40,
            "trail_trigger_step_percent": 5,
            "trail_stop_below_current_percent": 2.5,
            "ladder_buy_quantity": 0,
            "ladder_drop_steps_percent": [],
        }
        state = {"current_entry_order_id": "entry-1"}

        result = run_once(
            client,
            config,
            state,
            clock={"is_open": True},
        )

        self.assertEqual(result["status"], "managed")
        self.assertEqual(result["position_qty"], 10)
        self.assertEqual(client.submitted[0]["side"], "sell")
        self.assertEqual(client.submitted[0]["qty"], "10")
        self.assertEqual(client.submitted[0]["stop_price"], "44.85")
        self.assertEqual(state["active_stop_qty"], 10)
        self.assertTrue(result["recovery_stop_active"])
        self.assertAlmostEqual(result["planned_floor_price"], 61.4744)
        self.assertEqual(result["effective_stop_price"], 44.85)

    def test_recovery_stop_ratchets_up_but_not_down(self):
        config = {
            "trail_stop_below_current_percent": 2.5,
            "recovery_stop_enabled": True,
        }
        state = {}

        first_price, first_detail = effective_managed_stop_price(
            config, state, current_price=46, planned_floor=53.456
        )
        higher_price, _ = effective_managed_stop_price(
            config, state, current_price=48, planned_floor=53.456
        )
        retained_price, _ = effective_managed_stop_price(
            config, state, current_price=47, planned_floor=53.456
        )

        self.assertTrue(first_detail["recovery_stop_active"])
        self.assertEqual(first_price, 44.85)
        self.assertEqual(higher_price, 46.8)
        self.assertEqual(retained_price, 46.8)

    def test_atr_or_percent_initial_floor_is_capped(self):
        config = {
            "initial_stop_loss_percent": 20,
            "initial_stop_mode": "atr_or_percent",
            "initial_stop_atr_multiple": 2.5,
            "initial_stop_min_percent": 4,
            "initial_stop_max_percent": 10,
        }
        context = {"latest_bar": {"atr14": 1.0}}

        self.assertEqual(initial_floor_price(config, 100, context), 96.0)

    def test_trail_tiers_tighten_after_rungs(self):
        config = {
            "trail_trigger_step_percent": 5,
            "trail_stop_below_current_percent": 2.5,
            "trail_tiers": [
                {"gain_percent": 5, "trail_stop_below_current_percent": 2.5},
                {"gain_percent": 10, "trail_stop_below_current_percent": 2.0},
            ],
        }

        self.assertEqual(trail_below_current_percent(config, 1), 2.5)
        self.assertEqual(trail_below_current_percent(config, 2), 2.0)

    def test_order_risk_caps_shrink_quantity(self):
        client = FakeClient(account={"equity": "10000", "cash": "10000", "buying_power": "10000"})
        config = {"max_symbol_notional_percent": 25, "max_total_position_qty": 100}

        qty, detail = apply_order_risk_caps(client, config, 50, 100, current_qty=10)

        self.assertEqual(qty, 15)
        self.assertTrue(detail["risk_caps_enforced"])

    def test_cash_reserve_uses_cash_not_margin_buying_power(self):
        client = FakeClient(account={"equity": "50000", "cash": "-1000", "buying_power": "100000"})
        config = {"min_cash_balance_percent": 20}

        qty, detail = cash_protected_quantity(client, config, 10, 100)
        affordable_qty, share_detail = cash_available_share_count(client, config, 100)

        self.assertEqual(qty, 0)
        self.assertEqual(affordable_qty, 0)
        self.assertEqual(detail["min_cash_balance"], 10000)
        self.assertEqual(detail["available_notional"], 0)
        self.assertEqual(share_detail["available_notional"], 0)

    def test_zero_cash_reserve_still_cannot_use_margin(self):
        client = FakeClient(account={"equity": "50000", "cash": "0", "buying_power": "100000"})
        config = {"min_cash_balance_percent": 0}

        qty, detail = cash_protected_quantity(client, config, 10, 100)
        affordable_qty, share_detail = cash_available_share_count(client, config, 100)

        self.assertEqual(qty, 0)
        self.assertEqual(affordable_qty, 0)
        self.assertTrue(detail["margin_disabled"])
        self.assertTrue(share_detail["margin_disabled"])

    def test_ladder_notional_cap_shrinks_quantity(self):
        client = FakeClient(account={"equity": "100000", "cash": "100000", "buying_power": "100000"})
        config = {"entry_quantity": 100, "max_ladder_notional_percent": 5}
        state = {"base_position_qty": 100, "ladder_filled_notional": 4000}

        qty, detail = apply_ladder_risk_caps(client, config, state, 50, 100, current_qty=100)

        self.assertEqual(qty, 10)
        self.assertTrue(detail["ladder_caps_enforced"])
        self.assertEqual(detail["max_ladder_notional"], 5000)

    def test_ladder_position_multiple_cap_blocks_quantity(self):
        client = FakeClient()
        config = {"entry_quantity": 100, "max_ladder_position_multiple": 1.5}
        state = {"base_position_qty": 100}

        qty, detail = apply_ladder_risk_caps(client, config, state, 25, 100, current_qty=150)

        self.assertEqual(qty, 0)
        self.assertEqual(detail["max_ladder_position_qty"], 150)

    def test_adaptive_ladder_quantity_uses_base_fraction(self):
        config = {
            "adaptive_ladder_enabled": True,
            "ladder_size_fraction": 0.5,
            "volatility_ladder_scaling": True,
        }
        context = {"latest_bar": {"atr14": 1, "c": 100}, "market_ok": True}

        qty, detail = adaptive_ladder_quantity(config, 100, 80, context)

        self.assertEqual(qty, 40)
        self.assertEqual(detail["adjusted_qty"], 40)

    def test_adaptive_ladder_quantity_scales_down_high_volatility(self):
        config = {
            "adaptive_ladder_enabled": True,
            "ladder_size_fraction": 0.5,
            "volatility_ladder_scaling": True,
        }
        context = {"latest_bar": {"atr14": 5, "c": 100}, "market_ok": True}

        qty, detail = adaptive_ladder_quantity(config, 100, 80, context)

        self.assertEqual(qty, 20)
        self.assertEqual(detail["volatility_multiplier"], 0.5)

    def test_adaptive_ladder_limit_blocks_extreme_volatility(self):
        config = {
            "adaptive_ladder_enabled": True,
            "max_ladder_count": 2,
            "volatility_ladder_scaling": True,
        }
        context = {"latest_bar": {"atr14": 7, "c": 100}, "market_ok": True}

        limit, detail = adaptive_ladder_limit(config, context)

        self.assertEqual(limit, 0)
        self.assertEqual(detail["adjusted_max_ladder_count"], 0)


if __name__ == "__main__":
    unittest.main()
