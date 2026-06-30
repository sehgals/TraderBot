from traderbot.core_strategy_engine.engine import dynamic_entry_plan, handle_reentry, run_once
from traderbot.core_strategy_engine.strategies.base import Strategy
from traderbot.core_strategy_engine.strategies.registry import register_strategy


@register_strategy
class DynamicReentryStrategy(Strategy):
    strategy_type = "dynamic_reentry"

    def run_once(self):
        return run_once(self.client, self.config, self.state, clock=self.clock)


__all__ = [
    "DynamicReentryStrategy",
    "dynamic_entry_plan",
    "handle_reentry",
]
