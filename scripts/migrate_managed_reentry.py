import argparse
import datetime
import json
import shutil
from pathlib import Path


def load_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def save_json(path, data):
    with Path(path).open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, sort_keys=True)
        file.write("\n")


def migration_changes(config):
    desired = {
        "dynamic_entry_enabled": False,
        "reentry_enabled": True,
        "dynamic_reentry_enabled": True,
    }
    return {
        key: {"from": config.get(key), "to": value}
        for key, value in desired.items()
        if config.get(key) is not value
    }


def migrate(config_path, apply=False, backup_root=None):
    config_path = Path(config_path).resolve()
    project_root = config_path.parent.parent
    supervisor = load_json(config_path)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = Path(backup_root) if backup_root else (
        project_root / "runtime" / "backups" / f"managed_reentry_{timestamp}"
    )
    results = []
    for watcher in supervisor.get("managed_watchers", supervisor.get("watchers", [])):
        strategy_path = Path(watcher["config"])
        if not strategy_path.is_absolute():
            strategy_path = project_root / strategy_path
        strategy = load_json(strategy_path)
        changes = migration_changes(strategy)
        state_path = Path(watcher["state"])
        if not state_path.is_absolute():
            state_path = project_root / state_path
        state = load_json(state_path) if state_path.exists() else {}
        exit_status = "recorded" if state.get("last_exit_price") and state.get("last_exit_at") else "missing"
        results.append({
            "symbol": watcher["symbol"],
            "config": str(strategy_path),
            "changes": changes,
            "exit_record": exit_status,
        })
        if apply and changes:
            relative = strategy_path.resolve().relative_to(project_root.resolve())
            backup_path = backup_root / relative
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(strategy_path, backup_path)
            strategy["dynamic_entry_enabled"] = False
            strategy["reentry_enabled"] = True
            strategy["dynamic_reentry_enabled"] = True
            save_json(strategy_path, strategy)
    return results, backup_root if apply else None


def main():
    parser = argparse.ArgumentParser(
        description="Audit or migrate every managed watcher to the unified re-entry policy."
    )
    parser.add_argument("--config", default="config/watchers.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir")
    args = parser.parse_args()
    results, backup_path = migrate(args.config, args.apply, args.backup_dir)
    changed = [item for item in results if item["changes"]]
    missing = [item for item in results if item["exit_record"] == "missing"]
    print(f"Managed symbols: {len(results)}")
    print(f"Configs requiring changes: {len(changed)}")
    print(f"States without a complete exit record: {len(missing)}")
    for item in changed:
        summary = ", ".join(
            f"{key}={detail['from']}->{detail['to']}"
            for key, detail in item["changes"].items()
        )
        print(f"{item['symbol']}: {summary}; exit_record={item['exit_record']}")
    if backup_path:
        print(f"Backups: {backup_path}")
    elif changed:
        print("Dry run only; pass --apply to write changes.")


if __name__ == "__main__":
    main()
