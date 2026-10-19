"""Research-only weekly/monthly swing pullback for QuantConnect LEAN.

This is a separate hypothesis, not a port of TraderBot's production watcher.
Copy this file into a Python cloud project's main.py and run a backtest.
"""

from AlgorithmImports import *
from collections import Counter, deque
from datetime import time, timedelta
import math


class TraderBotSwingPullback(QCAlgorithm):
    def initialize(self):
        if self.live_mode:
            raise RuntimeError("Swing pullback research is backtest only")

        # Evaluate 2026 through the latest completed session as of 2026-10-12.
        # Earlier bars only warm up the higher-timeframe indicators.
        self.set_start_date(2026, 1, 1)
        self.set_end_date(2026, 10, 12)
        self.set_cash(100000)
        self.set_time_zone("America/New_York")
        self.set_brokerage_model(BrokerageName.ALPACA, AccountType.CASH)
        self.symbol = self.add_equity(
            "PLTR", Resolution.MINUTE, extended_market_hours=False,
            data_normalization_mode=DataNormalizationMode.ADJUSTED,
        ).symbol

        self.consolidate(self.symbol, timedelta(minutes=15), self.on_15_minute_bar)
        self.consolidate(self.symbol, Resolution.DAILY, self.on_daily_bar)
        self.consolidate(self.symbol, Calendar.WEEKLY, self.on_weekly_bar)
        self.consolidate(self.symbol, Calendar.MONTHLY, self.on_monthly_bar)

        self.weekly_emas = deque(maxlen=5)
        self.weekly_count = 0
        self.weekly_close = None
        self.monthly_closes = deque(maxlen=11)
        self.daily_highs = deque(maxlen=5)
        self.daily_lows = deque(maxlen=5)
        self.daily_emas = deque(maxlen=6)
        self.daily_close = None
        self.daily_ema20 = None
        self.daily_atr14 = None
        self.initial_true_ranges = deque(maxlen=14)
        self.below_daily_ema_count = 0
        self.exit_requested = False

        self.ema15 = None
        self.previous_15 = None
        self.previous_15_ema = None
        self.entry_pending = False
        self.exit_pending = False
        self.planned_stop = None
        self.stop_ticket = None
        self.last_exit_date = None
        self.research_counts = Counter()

        # Minute data also feeds the 15-minute, daily, weekly and monthly
        # consolidators. About 13 months primes the 10-month SMA and slope.
        self.set_warm_up(timedelta(days=400), Resolution.MINUTE)

    def on_weekly_bar(self, bar):
        close = float(bar.close)
        previous = self.weekly_emas[-1] if self.weekly_emas else close
        self.weekly_emas.append(previous + (2 / 11) * (close - previous))
        self.weekly_close = close
        self.weekly_count += 1

    def on_monthly_bar(self, bar):
        self.monthly_closes.append(float(bar.close))

    def on_daily_bar(self, bar):
        close, high, low = float(bar.close), float(bar.high), float(bar.low)
        true_range = high - low
        if self.daily_close is not None:
            true_range = max(
                true_range, abs(high - self.daily_close), abs(low - self.daily_close)
            )
        self.daily_close = close
        if self.daily_atr14 is None:
            self.initial_true_ranges.append(true_range)
            if len(self.initial_true_ranges) == 14:
                self.daily_atr14 = sum(self.initial_true_ranges) / 14
        else:
            self.daily_atr14 = (13 * self.daily_atr14 + true_range) / 14

        self.daily_ema20 = (
            close if self.daily_ema20 is None
            else self.daily_ema20 + (2 / 21) * (close - self.daily_ema20)
        )
        self.daily_emas.append(self.daily_ema20)
        self.daily_highs.append(high)
        self.daily_lows.append(low)

        if self.portfolio[self.symbol].quantity > 0:
            self.below_daily_ema_count = (
                self.below_daily_ema_count + 1
                if close < self.daily_ema20 else 0
            )
            if self.below_daily_ema_count >= 2:
                self.exit_requested = True
        else:
            self.below_daily_ema_count = 0

    def higher_timeframe_up(self):
        if self.weekly_count < 14 or len(self.weekly_emas) < 5:
            return False
        if len(self.monthly_closes) < 11:
            return False
        weekly_up = (
            self.weekly_close > self.weekly_emas[-1]
            and self.weekly_emas[-1] > self.weekly_emas[0]
        )
        closes = list(self.monthly_closes)
        monthly_sma = sum(closes[-10:]) / 10
        prior_monthly_sma = sum(closes[-11:-1]) / 10
        monthly_up = closes[-1] > monthly_sma > prior_monthly_sma
        return weekly_up and monthly_up

    def on_15_minute_bar(self, bar):
        close = float(bar.close)
        prior_ema = self.ema15
        self.ema15 = close if prior_ema is None else prior_ema + (2 / 10) * (close - prior_ema)
        prior_bar = self.previous_15
        prior_bar_ema = self.previous_15_ema
        if prior_bar is not None and prior_bar.end_time.date() != bar.end_time.date():
            prior_bar = None  # A reclaim needs a touch in the same session.
        self.previous_15 = bar
        self.previous_15_ema = self.ema15

        if self.is_warming_up:
            return
        self.research_counts["trade_window_bars"] += 1

        held = int(self.portfolio[self.symbol].quantity)
        if held > 0:
            if (self.exit_requested or not self.higher_timeframe_up()) and not self.exit_pending:
                self.exit_pending = True
                self.liquidate(self.symbol, tag="swing:trend_exit")
            return
        if self.entry_pending or self.exit_pending or self.last_exit_date == self.time.date():
            return
        if not self.higher_timeframe_up() or len(self.daily_emas) < 6:
            return
        self.research_counts["higher_trend_pass"] += 1
        if len(self.daily_highs) < 5 or not self.daily_atr14 or self.daily_atr14 <= 0:
            return
        if prior_bar is None or prior_bar_ema is None:
            return
        if bar.end_time.time() > time(15, 30):
            return

        recent_high = max(self.daily_highs)
        dip_atr = (recent_high - close) / self.daily_atr14
        dip_percent = 100 * (recent_high - close) / recent_high
        daily_support_holds = close >= self.daily_ema20 - 0.25 * self.daily_atr14
        daily_trend_up = self.daily_emas[-1] > self.daily_emas[0]
        reclaimed = (
            float(prior_bar.low) <= 1.005 * prior_bar_ema
            and close > self.ema15
            and close > float(prior_bar.close)
        )
        if not (
            0.5 <= dip_atr <= 1.5
            and dip_percent <= 3.0
            and daily_support_holds
            and daily_trend_up
            and reclaimed
        ):
            return
        self.research_counts["dip_reclaim_pass"] += 1

        # Keep the protective stop below recent daily structure. Skip entries
        # whose stop would be farther than 8% from the signal price.
        stop = min(self.daily_lows) - 0.1 * self.daily_atr14
        risk_per_share = close - stop
        if not 0 < risk_per_share <= 0.08 * close:
            return
        equity = float(self.portfolio.total_portfolio_value)
        qty = min(
            math.floor(0.0025 * equity / risk_per_share),
            math.floor(0.10 * equity / close),
        )
        if qty < 1:
            return
        self.planned_stop = stop
        self.entry_pending = True
        self.research_counts["entry_orders"] += 1
        ticket = self.market_order(
            self.symbol, qty, asynchronous=True, tag="swing:pullback_entry"
        )
        if ticket.status == OrderStatus.INVALID:
            self.entry_pending = False
            self.planned_stop = None

    def on_order_event(self, event):
        order = self.transactions.get_order_by_id(event.order_id)
        if order is None:
            return
        tag = str(order.tag or "")

        if tag == "swing:pullback_entry":
            if event.fill_quantity > 0:
                self.research_counts["entry_fills"] += 1
                held = int(self.portfolio[self.symbol].quantity)
                fill = float(event.fill_price)
                if self.planned_stop is None or fill <= self.planned_stop:
                    self.liquidate(self.symbol, tag="swing:invalid_entry_fill")
                elif held > 0:
                    # A partially filled entry is protected for shares held so far.
                    self.replace_stop(-held, self.planned_stop)
            if event.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.INVALID):
                self.entry_pending = False
            return

        if tag == "swing:protective_stop" and event.fill_quantity < 0:
            if int(self.portfolio[self.symbol].quantity) == 0:
                self.stop_ticket = None
                self.last_exit_date = self.time.date()
                self.exit_requested = False
                self.exit_pending = False
            return

        if tag in ("swing:trend_exit", "swing:invalid_entry_fill"):
            if event.fill_quantity < 0 and int(self.portfolio[self.symbol].quantity) == 0:
                self.stop_ticket = None
                self.last_exit_date = self.time.date()
                self.exit_requested = False
            if event.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.INVALID):
                self.exit_pending = False

    def replace_stop(self, quantity, price):
        price = round(price, 2)
        if self.stop_ticket is None:
            self.stop_ticket = self.stop_market_order(
                self.symbol, quantity, price, tag="swing:protective_stop"
            )
            if self.stop_ticket.status == OrderStatus.INVALID:
                raise RuntimeError("Swing entry has no protective stop")
        else:
            change = UpdateOrderFields()
            change.quantity = quantity
            change.stop_price = price
            if not self.stop_ticket.update(change).is_success:
                raise RuntimeError("Could not resize protective stop")

    def on_end_of_algorithm(self):
        self.log(f"SWING_RESEARCH_COUNTS {dict(self.research_counts)}")
