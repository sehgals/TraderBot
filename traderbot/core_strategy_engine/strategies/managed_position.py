from traderbot.core_strategy_engine.engine import run_once
from traderbot.core_strategy_engine.strategies.base import Strategy
from traderbot.core_strategy_engine.strategies.registry import register_strategy


@register_strategy
class ManagedDynamicReentryStrategy(Strategy):
    strategy_type = "managed_dynamic_reentry"

    def run_once(self):
        return run_once(self.client, self.config, self.state, clock=self.clock)


@register_strategy
class ManagedTrailingFloorStrategy(ManagedDynamicReentryStrategy):
    strategy_type = "managed_trailing_floor"


__all__ = [
    "ManagedDynamicReentryStrategy",
    "ManagedTrailingFloorStrategy",
    "run_once",
]
