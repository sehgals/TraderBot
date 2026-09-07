from pathlib import Path
import sys

# Keep optional web dependencies separate from the trading runtime.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/dashboard-deps"))

from traderbot.dashboard.server import main

if __name__ == "__main__":
    main()
