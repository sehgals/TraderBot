import unittest

from traderbot.core_strategy_engine.engine import (
    adaptive_ladder_limit,
    adaptive_ladder_quantity,
    apply_ladder_risk_caps,
    apply_order_risk_caps,
    initial_floor_price,
    reconcile_flat_position_state,
    trail_below_current_percent,
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


class RiskControlTests(unittest.TestCase):
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
