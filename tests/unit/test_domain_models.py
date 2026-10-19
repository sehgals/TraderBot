"""Unit tests for Phase 4: Domain Models and Value Objects."""

import unittest

from traderbot.domain.models import (
    Bar,
    Order,
    OrderSide,
    OrderType,
    Position,
    PositionEpisode,
    PositionState,
)


class DomainModelsTests(unittest.TestCase):
    def test_bar_from_alpaca_and_to_dict(self):
        payload = {
            "t": "2026-10-19T14:30:00Z",
            "o": 100.5,
            "h": 102.0,
            "l": 99.8,
            "c": 101.5,
            "v": 54000,
            "vw": 101.2,
        }
        bar = Bar.from_alpaca(payload)
        self.assertEqual(bar.timestamp, "2026-10-19T14:30:00Z")
        self.assertEqual(bar.open, 100.5)
        self.assertEqual(bar.high, 102.0)
        self.assertEqual(bar.low, 99.8)
        self.assertEqual(bar.close, 101.5)
        self.assertEqual(bar.volume, 54000)
        self.assertEqual(bar.vwap, 101.2)
        self.assertEqual(bar.to_dict(), payload)

    def test_position_calculations(self):
        payload = {
            "symbol": "NVDA",
            "qty": "10",
            "avg_entry_price": "100.0",
            "current_price": "110.0",
        }
        pos = Position.from_alpaca(payload)
        self.assertEqual(pos.symbol, "NVDA")
        self.assertEqual(pos.quantity, 10)
        self.assertEqual(pos.avg_entry_price, 100.0)
        self.assertEqual(pos.current_price, 110.0)
        self.assertEqual(pos.market_value, 1100.0)
        self.assertEqual(pos.unrealized_pnl, 100.0)
        self.assertEqual(pos.unrealized_pnl_percent, 10.0)

    def test_order_from_alpaca_and_enums(self):
        payload = {
            "id": "order-12345",
            "client_order_id": "client-abc",
            "symbol": "AAPL",
            "side": "buy",
            "type": "limit",
            "qty": "5",
            "limit_price": "180.5",
            "status": "filled",
            "filled_avg_price": "180.25",
            "filled_qty": "5",
        }
        order = Order.from_alpaca(payload)
        self.assertEqual(order.order_id, "order-12345")
        self.assertEqual(order.client_order_id, "client-abc")
        self.assertEqual(order.symbol, "AAPL")
        self.assertEqual(order.side, OrderSide.BUY)
        self.assertEqual(order.order_type, OrderType.LIMIT)
        self.assertEqual(order.limit_price, 180.5)
        self.assertEqual(order.filled_avg_price, 180.25)
        self.assertEqual(order.status, "filled")

    def test_position_episode_extremes_and_r_multiples(self):
        episode = PositionEpisode(
            episode_id="ep-1",
            symbol="MSFT",
            fill_price=400.0,
            initial_stop_price=380.0,
            initial_risk_per_share=20.0,  # 1R = $20
        )
        self.assertEqual(episode.state, PositionState.OPEN)
        self.assertEqual(episode.high_water_price, 400.0)
        self.assertEqual(episode.low_water_price, 400.0)

        # Price rallies to $440 (+2R) then dips to $390 (-0.5R)
        episode.update_price_extremes(440.0)
        self.assertEqual(episode.high_water_price, 440.0)
        self.assertEqual(episode.mfe_percent, 10.0)
        self.assertEqual(episode.mfe_r, 2.0)  # $40 / $20 = 2.0R

        episode.update_price_extremes(390.0)
        self.assertEqual(episode.low_water_price, 390.0)
        self.assertEqual(episode.mae_percent, 2.5)
        self.assertEqual(episode.mae_r, 0.5)  # $10 / $20 = 0.5R

        d = episode.to_dict()
        self.assertEqual(d["max_price"], 440.0)
        self.assertEqual(d["min_price"], 390.0)
        self.assertEqual(d["state"], "OPEN")

        restored = PositionEpisode.from_dict(d)
        self.assertEqual(restored.episode_id, "ep-1")
        self.assertEqual(restored.high_water_price, 440.0)


if __name__ == "__main__":
    unittest.main()
