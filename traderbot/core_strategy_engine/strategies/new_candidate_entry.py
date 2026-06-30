from traderbot.core_strategy_engine.engine import handle_dynamic_flat_entry, run_once
from traderbot.core_strategy_engine.strategies.base import Strategy
from traderbot.core_strategy_engine.strategies.registry import register_strategy


@register_strategy
class NewCandidateDynamicEntryStrategy(Strategy):
    strategy_type = "new_candidate_dynamic_entry"

    def run_once(self):
        return run_once(self.client, self.config, self.state, clock=self.clock)


__all__ = [
    "NewCandidateDynamicEntryStrategy",
    "handle_dynamic_flat_entry",
]
