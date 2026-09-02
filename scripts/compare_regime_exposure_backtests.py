import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traderbot.backtester.regime_comparison import compare_regime_backtests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--unbanded", required=True)
    parser.add_argument("--banded", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-trades", type=int, default=20)
    args = parser.parse_args()
    result = compare_regime_backtests(
        json.loads(Path(args.unbanded).read_text(encoding="utf-8")),
        json.loads(Path(args.banded).read_text(encoding="utf-8")),
        minimum_trades=args.minimum_trades,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"accepted": result["accepted"], "reasons": result["reasons"]}))
    return 0 if result["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
