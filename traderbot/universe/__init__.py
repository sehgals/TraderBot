"""TraderBot Universe management, archetypes, and sector resolution."""

from traderbot.universe.archetypes import (
    DEFAULT_ARCHETYPES,
    load_archetypes,
    resolve_watcher_paths,
    resolve_watcher_strategy_defaults,
)
from traderbot.universe.onboarding import (
    add_stock_to_universe,
    get_available_archetypes,
    inspect_stock,
    remove_stock_from_universe,
    validate_symbol_format,
)
from traderbot.universe.sector_resolver import (
    ETF_TO_SECTOR_NAME,
    SECTOR_NAME_TO_ETF,
    SYMBOL_TO_SECTOR_ETF,
    etf_to_sector_name,
    resolve_sector_etf,
    sector_name_to_etf,
)

__all__ = [
    "DEFAULT_ARCHETYPES",
    "ETF_TO_SECTOR_NAME",
    "SECTOR_NAME_TO_ETF",
    "SYMBOL_TO_SECTOR_ETF",
    "add_stock_to_universe",
    "etf_to_sector_name",
    "get_available_archetypes",
    "inspect_stock",
    "load_archetypes",
    "remove_stock_from_universe",
    "resolve_sector_etf",
    "resolve_watcher_paths",
    "resolve_watcher_strategy_defaults",
    "sector_name_to_etf",
    "validate_symbol_format",
]
