import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from traderbot.shadow_deployment import PROMOTION_STAGES, promote_stage


def atomic_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-stage", choices=PROMOTION_STAGES[1:], required=True)
    parser.add_argument("--config", default="config/watchers.json")
    parser.add_argument("--state", default="runtime/state/portfolio_allocation_state.json")
    args = parser.parse_args()
    config_path = Path(args.config)
    state_path = Path(args.state)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    updated_config, updated_state, decision = promote_stage(
        config, state, args.target_stage
    )
    if not decision["eligible"]:
        print(json.dumps(decision))
        return 2
    atomic_json(config_path, updated_config)
    atomic_json(state_path, updated_state)
    print(json.dumps({
        **decision,
        "restart_required": "Use TraderBot_Watcher_Supervisor; do not launch manually",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
