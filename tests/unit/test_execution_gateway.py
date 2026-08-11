import unittest

from traderbot.broker.execution_gateway import ExecutionGateway


class FakeBroker:
    def __init__(self):
        self.orders = {}
        self._position = {"symbol": "WAT", "qty": "10"}
        self.submitted = []
        self.canceled = []
        self.replaced = []

    def order_by_client_order_id(self, client_order_id):
        return next(
            (
                order
                for order in self.orders.values()
                if order.get("client_order_id") == client_order_id
            ),
            None,
        )

    def order(self, order_id):
        return self.orders[order_id]

    def submit_order(self, payload):
        order = {"id": f"order-{len(self.orders) + 1}", **payload}
        self.orders[order["id"]] = order
        self.submitted.append(payload)
        return order

    def cancel_order(self, order_id):
        self.canceled.append(order_id)

    def replace_order(self, order_id, payload):
        self.replaced.append((order_id, payload))
        return {**self.orders[order_id], **payload}

    def positions(self):
        return [{"symbol": "WAT"}]

    def position(self, symbol):
        return self._position if self._position.get("symbol") == symbol else None

    def open_orders(self):
        return [
            order
            for order in self.orders.values()
            if order.get("status", "accepted") in (
                "new", "accepted", "pending_new", "partially_filled"
            )
        ]


class ExecutionGatewayTests(unittest.TestCase):
    def test_deterministic_client_order_id_is_deduplicated(self):
        broker = FakeBroker()
        gateway = ExecutionGateway(broker)
        payload = {
            "symbol": "WAT",
            "qty": "5",
            "side": "sell",
            "type": "market",
            "client_order_id": "health-WAT-reduce-1",
        }

        first = gateway.submit_order(payload)
        second = gateway.submit_order(payload)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(broker.submitted), 1)

    def test_cancel_and_replace_resolve_symbol_before_mutation(self):
        broker = FakeBroker()
        order = broker.submit_order(
            {"symbol": "WAT", "qty": "5", "side": "sell", "type": "stop"}
        )
        gateway = ExecutionGateway(broker)

        gateway.replace_order(order["id"], {"qty": "4"})
        gateway.cancel_order(order["id"])

        self.assertEqual(broker.replaced, [(order["id"], {"qty": "4"})])
        self.assertEqual(broker.canceled, [order["id"]])

    def test_read_operations_are_delegated(self):
        gateway = ExecutionGateway(FakeBroker())

        self.assertEqual(gateway.positions(), [{"symbol": "WAT"}])

    def test_competing_non_stop_sell_is_blocked(self):
        broker = FakeBroker()
        gateway = ExecutionGateway(broker)
        gateway.submit_order(
            {
                "symbol": "WAT",
                "qty": "5",
                "side": "sell",
                "type": "market",
                "client_order_id": "margin-reduce-1",
            }
        )

        with self.assertRaisesRegex(RuntimeError, "existing non-stop sell"):
            gateway.submit_order(
                {
                    "symbol": "WAT",
                    "qty": "5",
                    "side": "sell",
                    "type": "market",
                    "client_order_id": "health-reduce-1",
                }
            )

    def test_sell_quantity_cannot_exceed_broker_position(self):
        gateway = ExecutionGateway(FakeBroker())

        with self.assertRaisesRegex(RuntimeError, "broker position has 10"):
            gateway.submit_order(
                {
                    "symbol": "WAT",
                    "qty": "11",
                    "side": "sell",
                    "type": "market",
                }
            )


if __name__ == "__main__":
    unittest.main()
