from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
RUNTIME_DIR = PROJECT_ROOT / "runtime"
STATE_DIR = RUNTIME_DIR / "state"
LOG_DIR = RUNTIME_DIR / "logs"
STRATEGY_CONFIG_DIR = (
    PROJECT_ROOT / "traderbot" / "core_strategy_engine" / "strategies" / "configs"
)


__all__ = [
    "CONFIG_DIR",
    "LOG_DIR",
    "PROJECT_ROOT",
    "RUNTIME_DIR",
    "STATE_DIR",
    "STRATEGY_CONFIG_DIR",
]
