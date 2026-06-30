from traderbot.core_strategy_engine.strategies.base import DEFAULT_STRATEGY_TYPE


STRATEGY_REGISTRY = {}
_BUILTINS_LOADED = False


def register_strategy(strategy_cls):
    strategy_type = getattr(strategy_cls, "strategy_type", None)
    if not strategy_type:
        raise ValueError(f"{strategy_cls.__name__} must define strategy_type")
    if strategy_type in STRATEGY_REGISTRY:
        raise ValueError(f"Duplicate strategy_type registered: {strategy_type}")
    STRATEGY_REGISTRY[strategy_type] = strategy_cls
    return strategy_cls


def load_builtin_strategies():
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    from traderbot.core_strategy_engine.strategies import dynamic_reentry  # noqa: F401
    from traderbot.core_strategy_engine.strategies import managed_position  # noqa: F401
    from traderbot.core_strategy_engine.strategies import new_candidate_entry  # noqa: F401

    _BUILTINS_LOADED = True


def resolve_strategy(strategy_type=None):
    load_builtin_strategies()
    selected = strategy_type or DEFAULT_STRATEGY_TYPE
    try:
        return STRATEGY_REGISTRY[selected]
    except KeyError as exc:
        available = ", ".join(sorted(STRATEGY_REGISTRY))
        raise ValueError(f"Unknown strategy_type {selected!r}. Available: {available}") from exc
