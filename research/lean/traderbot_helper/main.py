"""LEAN research companion for TraderBot's breakout and winner exit rules.

Backtest only. This deliberately covers one entry family and one exit policy;
it is not a port of the production watcher or an order execution replacement.
"""

from collections import deque
from datetime import timedelta
import math
from typing import Sequence

from AlgorithmImports import *


def breakout_ready(
    close: float,
    volume: float,
    prior_highs: Sequence[float],
    prior_volumes: Sequence[float],
    ema21: float | None,
    hourly_atr: float | None,
) -> bool:
    """Use only completed prior bars for the breakout baseline."""
    if len(prior_highs) < 20 or len(prior_volumes) < 20:
        return False
    if not hourly_atr or hourly_atr <= 0 or not ema21:
        return False
    average_volume = sum(prior_volumes) / len(prior_volumes)
    return (
        close > max(prior_highs)
        and close > ema21
        and average_volume > 0
        and volume >= 1.5 * average_volume
    )


def entry_quantity(equity: float, price: float, risk_per_share: float) -> int:
    """Cap one entry at 1% account risk and 10% notional; require a runner."""
    if min(equity, price, risk_per_share) <= 0:
        return 0
    quantity = min(
        math.floor(0.01 * equity / risk_per_share),
        math.floor(0.10 * equity / price),
    )
    return quantity if quantity >= 2 else 0


def partial_quantity(initial_quantity: int, held_quantity: int) -> int:
    if initial_quantity < 2 or held_quantity < 2:
        return 0
    return min(math.floor(initial_quantity * 0.5), held_quantity - 1)


def ratcheted_runner_stop(
    previous_stop: float,
    initial_stop: float,
    highest_price: float,
    hourly_atr: float,
) -> float:
    if hourly_atr <= 0:
        return max(previous_stop, initial_stop)
    return max(previous_stop, initial_stop, highest_price - 2.5 * hourly_atr)


class TraderBotHelper(QCAlgorithm):
    def initialize(self) -> None:
        if self.live_mode:
            raise RuntimeError("TraderBotHelper is a research backtest only")
        self.set_start_date(2024, 1, 1)
        self.set_end_date(2025, 12, 31)
        self.set_cash(100000)
        self.set_time_zone("America/New_York")
        self.set_brokerage_model(BrokerageName.ALPACA, AccountType.CASH)
        self._symbol = self.add_equity(
            "MU", Resolution.MINUTE, extended_market_hours=False,
            data_normalization_mode=DataNormalizationMode.RAW,
        ).symbol
        self.consolidate(self._symbol, timedelta(minutes=5), self.on_five_minute_bar)
        # Anchor hourly bars at 09:30 ET, matching the market-open hour.
        self.hour_consolidator = TradeBarConsolidator(
            timedelta(hours=1), start_time=timedelta(hours=9, minutes=30)
        )
        self.hour_consolidator.data_consolidated += self.on_hour_bar
        self.subscription_manager.add_consolidator(self._symbol, self.hour_consolidator)

        self.prior_highs = deque(maxlen=20)
        self.prior_volumes = deque(maxlen=20)
        self.ema21 = None
        self.hour_true_ranges = deque(maxlen=14)
        self.hourly_atr = None
        self.previous_hour_close = None

        self.entry_pending = False
        self.partial_pending = False
        self.partial_remaining = 0
        self.partial_ticket = None
        self.partial_done = False
        self.stop_ticket = None
        self.stop_price = None
        self.planned_risk = None
        self.initial_risk = None
        self.initial_stop = None
        self.initial_quantity = 0
        self.highest_runner_price = 0
        self.event_logs = 0

    def on_hour_bar(self, sender: object, bar: TradeBar) -> None:
        high, low, close = float(bar.high), float(bar.low), float(bar.close)
        true_range = high - low
        if self.previous_hour_close is not None:
            true_range = max(
                true_range,
                abs(high - self.previous_hour_close),
                abs(low - self.previous_hour_close),
            )
        self.previous_hour_close = close
        if self.hourly_atr is None:
            self.hour_true_ranges.append(true_range)
            if len(self.hour_true_ranges) == 14:
                self.hourly_atr = sum(self.hour_true_ranges) / 14
        else:
            self.hourly_atr = (13 * self.hourly_atr + true_range) / 14

    def on_five_minute_bar(self, bar: TradeBar) -> None:
        close, high, volume = float(bar.close), float(bar.high), float(bar.volume)
        holding = self.portfolio[self._symbol]
        held = int(holding.quantity)

        if held > 0 and self.partial_done:
            self.highest_runner_price = max(self.highest_runner_price, high)
            candidate = ratcheted_runner_stop(
                self.stop_price or 0, self.initial_stop or 0,
                self.highest_runner_price, self.hourly_atr or 0,
            )
            if candidate > (self.stop_price or 0) and candidate < close:
                self.update_stop(-held, candidate)

        if (
            held >= 2 and not self.partial_done and not self.partial_pending
            and self.initial_risk and self.hourly_atr
            and close >= float(holding.average_price) + 2 * self.initial_risk
        ):
            sell = partial_quantity(self.initial_quantity, held)
            runner = held - sell
            # Reserve the runner's protection before submitting the tranche.
            if sell and self.update_stop(-runner, self.stop_price):
                self.partial_remaining = sell
                self.partial_pending = True
                self.partial_ticket = self.market_order(
                    self._symbol, -sell, asynchronous=True, tag="tb:profit_tranche_2r"
                )
                if self.partial_ticket.status == OrderStatus.INVALID:
                    self.restore_full_stop()

        if (
            held == 0 and not self.entry_pending and not self.partial_pending
            and breakout_ready(
                close, volume, self.prior_highs, self.prior_volumes,
                self.ema21, self.hourly_atr,
            )
        ):
            risk = 1.5 * self.hourly_atr
            quantity = entry_quantity(float(self.portfolio.total_portfolio_value), close, risk)
            if quantity:
                self.planned_risk = risk
                self.entry_pending = True
                ticket = self.market_order(
                    self._symbol, quantity, asynchronous=True, tag="tb:breakout_entry"
                )
                if ticket.status == OrderStatus.INVALID:
                    self.entry_pending = False

        self.prior_highs.append(high)
        self.prior_volumes.append(volume)
        self.ema21 = close if self.ema21 is None else self.ema21 + (2 / 22) * (close - self.ema21)

    def update_stop(self, quantity: int, price: float | None) -> bool:
        if price is None or price <= 0:
            return False
        price = round(price, 2)
        if self.stop_ticket is None:
            self.stop_ticket = self.stop_market_order(
                self._symbol, quantity, price, tag="tb:protective_stop"
            )
            if self.stop_ticket.status == OrderStatus.INVALID:
                self.stop_ticket = None
                return False
        else:
            changes = UpdateOrderFields()
            changes.quantity = quantity
            changes.stop_price = price
            if not self.stop_ticket.update(changes).is_success:
                return False
        self.stop_price = price
        return True

    def on_order_event(self, event: OrderEvent) -> None:
        order = self.transactions.get_order_by_id(event.order_id)
        if order is None:
            return
        tag = str(order.tag or "")
        filled = int(abs(event.fill_quantity))

        if tag == "tb:breakout_entry":
            if filled and event.fill_quantity > 0:
                held = int(self.portfolio[self._symbol].quantity)
                self.initial_quantity += filled
                self.initial_risk = self.planned_risk
                self.initial_stop = float(self.portfolio[self._symbol].average_price) - self.initial_risk
                self.partial_done = False
                self.highest_runner_price = 0
                if not self.update_stop(-held, self.initial_stop):
                    raise RuntimeError("research entry has no protective stop")
                self.log_event(f"entry_fill {self.time} {held} {event.fill_price}")
            if event.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.INVALID):
                self.entry_pending = False
            return

        if tag == "tb:profit_tranche_2r":
            if filled and event.fill_quantity < 0:
                self.partial_remaining = max(0, self.partial_remaining - filled)
                held = int(self.portfolio[self._symbol].quantity)
                protected = held - self.partial_remaining
                if protected > 0:
                    self.update_stop(-protected, self.stop_price)
                self.log_event(f"partial_fill {self.time} {filled} {event.fill_price}")
            if event.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.INVALID):
                self.restore_full_stop()
                if event.status == OrderStatus.FILLED:
                    self.partial_done = True
                    self.highest_runner_price = float(event.fill_price)
            return

        if tag == "tb:protective_stop" and filled and event.fill_quantity < 0:
            self.log_event(f"stop_fill {self.time} {filled} {event.fill_price}")
            if event.status == OrderStatus.FILLED:
                self.stop_ticket = None
                self.stop_price = None
            if self.partial_pending and self.partial_ticket is not None:
                self.partial_ticket.cancel("runner stop filled")
            if int(self.portfolio[self._symbol].quantity) == 0:
                self.reset_position()

    def restore_full_stop(self) -> None:
        self.partial_pending = False
        self.partial_remaining = 0
        self.partial_ticket = None
        held = int(self.portfolio[self._symbol].quantity)
        if held > 0 and not self.update_stop(-held, self.stop_price or self.initial_stop):
            raise RuntimeError("research runner has no protective stop")

    def log_event(self, message: str) -> None:
        # The Free cloud tier has a small per-backtest log allowance. Order
        # tickets retain the complete tagged order history in the results.
        if self.event_logs < 50:
            self.log(f"TB_EVENT {message}")
            self.event_logs += 1

    def reset_position(self) -> None:
        self.stop_ticket = None
        self.stop_price = None
        self.planned_risk = None
        self.initial_risk = None
        self.initial_stop = None
        self.initial_quantity = 0
        self.partial_done = False
        self.partial_pending = False
        self.partial_remaining = 0
        self.partial_ticket = None
        self.highest_runner_price = 0
