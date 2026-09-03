import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traderbot.backtester.model_admission import evaluate_model_admission


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing", required=True)
    parser.add_argument("--isolated", required=True)
    parser.add_argument("--combined", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--minimum-trades", type=int, default=20)
    parser.add_argument("--maximum-correlation", type=float, default=0.8)
    args = parser.parse_args()
    load = lambda path: json.loads(Path(path).read_text(encoding="utf-8"))
    result = evaluate_model_admission(
        load(args.existing), load(args.isolated), load(args.combined),
        args.minimum_trades, args.maximum_correlation,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"admitted": result["admitted"], "reasons": result["reasons"]}))
    return 0 if result["admitted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
