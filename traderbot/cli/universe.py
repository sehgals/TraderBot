"""CLI for zero-downtime stock onboarding, validation, and universe management.

Usage:
    python -m traderbot.cli.universe list
    python -m traderbot.cli.universe add NVDA --archetype large_cap_tech
    python -m traderbot.cli.universe validate NVDA
    python -m traderbot.cli.universe remove NVDA
"""

import argparse
import datetime
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

from traderbot.core_strategy_engine.engine import (
    AlpacaClient,
    iso_utc,
    load_env,
    load_json,
    save_json,
)
from traderbot.core_strategy_engine.rollout import get_symbol_rollout_cohort
from traderbot.universe.archetypes import DEFAULT_ARCHETYPES, load_archetypes
from traderbot.universe.sector_resolver import (
    etf_to_sector_name,
    resolve_sector_etf,
)

DEFAULT_CONFIG_PATH = Path("config/watchers.json")


def get_alpaca_client() -> Optional[AlpacaClient]:
    """Initialize AlpacaClient safely if environment credentials are present."""
    try:
        load_env()
        return AlpacaClient()
    except Exception:
        return None


def cmd_list(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: configuration file not found at {config_path}", file=sys.stderr)
        return 1

    config = load_json(config_path, {})
    salt = config.get("stage_3_rollout", {}).get("salt", "stage3_canary")
    canary_fraction = float(config.get("stage_3_rollout", {}).get("canary_fraction", 0.25))

    managed = config.get("managed_watchers", config.get("watchers", []))
    candidates = config.get("new_watchers", [])

    print(f"\nTraderBot Universe ({len(managed)} Managed, {len(candidates)} Candidates):")
    print("-" * 75)
    print(f"{'SYMBOL':<8} {'GROUP':<10} {'ARCHETYPE':<18} {'SECTOR ETF':<12} {'COHORT':<10}")
    print("-" * 75)

    all_watchers = [
        *(({**w, "group": "managed"}) for w in managed),
        *(({**w, "group": "new"}) for w in candidates),
    ]

    for w in sorted(all_watchers, key=lambda x: (x["group"] != "managed", x["symbol"])):
        sym = w["symbol"]
        grp = w["group"]
        archetype = w.get("archetype", "default" if not w.get("config") else "custom_file")
        sector = resolve_sector_etf(sym, config)
        cohort = get_symbol_rollout_cohort(sym, config).upper()
        print(f"{sym:<8} {grp:<10} {archetype:<18} {sector:<12} {cohort:<10}")

    print("-" * 75)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    symbol = str(args.symbol).upper().strip()
    client = get_alpaca_client()
    config = load_json(Path(args.config), {}) if Path(args.config).exists() else {}

    print(f"\nValidating symbol '{symbol}' for TraderBot universe:")
    print("-" * 60)

    # 1. Sector mapping
    sector_etf = resolve_sector_etf(symbol, config)
    sector_name = etf_to_sector_name(sector_etf)
    print(f"[*] Sector Resolution : {sector_etf} ({sector_name})")

    # 2. Rollout cohort
    cohort = get_symbol_rollout_cohort(symbol, config).upper()
    print(f"[*] Rollout Cohort    : {cohort}")

    if not client:
        print("[!] Alpaca credentials not configured; skipping live broker check.")
        return 0

    # 3. Alpaca Asset check
    try:
        asset = client.asset(symbol)
        if not asset:
            print(f"[X] Alpaca Asset Check: FAILED (Symbol '{symbol}' not found on exchange)")
            return 1
        is_tradable = asset.get("tradable", False)
        status = asset.get("status", "unknown")
        fractionable = asset.get("fractionable", False)
        marginable = asset.get("marginable", False)
        print(f"[+] Alpaca Asset Check: ACTIVE ({asset.get('name', 'US Equity')})")
        print(f"    - Tradable: {is_tradable} | Status: {status} | Marginable: {marginable} | Fractionable: {fractionable}")
        if not is_tradable or status != "active":
            print(f"[X] Asset is not active/tradable on Alpaca.")
            return 1
    except Exception as exc:
        print(f"[!] Alpaca Asset Check Error: {exc}")

    # 4. Market Data check
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        start = iso_utc(now - datetime.timedelta(days=7))
        end = iso_utc(now)
        bars = client.stock_bars(symbol, start, end, "5Min") or []
        print(f"[+] Market Data Check : OK ({len(bars)} 5Min bars fetched over last 7 days)")
        if not bars:
            print("[!] Warning: Zero bars returned. Check symbol or market data subscription.")
    except Exception as exc:
        print(f"[!] Market Data Check Error: {exc}")

    print("-" * 60)
    print(f"[+] '{symbol}' is READY for onboarding.\n")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    symbol = str(args.symbol).upper().strip()
    config_path = Path(args.config)
    group = "new" if args.group == "candidate" else args.group
    archetype = args.archetype

    available_archetypes = load_archetypes(config_path.parent.parent)
    if archetype not in available_archetypes:
        print(f"Error: Unknown archetype '{archetype}'. Available: {list(available_archetypes.keys())}", file=sys.stderr)
        return 1

    # Run validation first
    if not args.skip_validation:
        val_code = cmd_validate(args)
        if val_code != 0:
            print("[X] Pre-flight validation failed. Aborting add.", file=sys.stderr)
            return val_code

    config = load_json(config_path, {})
    managed = config.setdefault("managed_watchers", [])
    candidates = config.setdefault("new_watchers", [])

    if any(w.get("symbol") == symbol for w in managed):
        print(f"[!] Symbol '{symbol}' is already enrolled in managed_watchers.")
        return 0
    if any(w.get("symbol") == symbol for w in candidates):
        print(f"[!] Symbol '{symbol}' is already enrolled in new_watchers (candidates).")
        return 0

    entry = {"symbol": symbol}
    if archetype != "default":
        entry["archetype"] = archetype

    target_list = candidates if group == "new" else managed
    target_list.append(entry)

    if args.dry_run:
        print(f"[DRY-RUN] Would add to {group}_watchers: {json.dumps(entry, indent=2)}")
        return 0

    save_json(config_path, config)
    print(f"[SUCCESS] Enrolled '{symbol}' into {group}_watchers with archetype '{archetype}'.")
    print(f"          WatcherListReloader will pick up the update on the next scheduler cycle.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    symbol = str(args.symbol).upper().strip()
    config_path = Path(args.config)
    config = load_json(config_path, {})

    managed_before = len(config.get("managed_watchers", []))
    new_before = len(config.get("new_watchers", []))

    config["managed_watchers"] = [w for w in config.get("managed_watchers", []) if w.get("symbol") != symbol]
    config["new_watchers"] = [w for w in config.get("new_watchers", []) if w.get("symbol") != symbol]

    managed_removed = managed_before - len(config["managed_watchers"])
    new_removed = new_before - len(config["new_watchers"])

    if managed_removed == 0 and new_removed == 0:
        print(f"Symbol '{symbol}' was not found in watchers configuration.")
        return 0

    save_json(config_path, config)
    print(f"[SUCCESS] Removed '{symbol}' from watchers configuration.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="TraderBot Universe Management CLI")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to watchers.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: list
    subparsers.add_parser("list", help="List all symbols, archetypes, and cohorts")

    # Subcommand: validate
    val_parser = subparsers.add_parser("validate", help="Validate a symbol for connectivity & data")
    val_parser.add_argument("symbol", help="Stock ticker symbol (e.g. NVDA)")

    # Subcommand: add
    add_parser = subparsers.add_parser("add", help="Add a new symbol with archetype")
    add_parser.add_argument("symbol", help="Stock ticker symbol (e.g. NVDA)")
    add_parser.add_argument("--archetype", default="default", help="Risk archetype (default, large_cap_tech, high_beta_growth, defensive_value)")
    add_parser.add_argument("--group", choices=["candidate", "managed"], default="candidate", help="Group: candidate (new) or managed")
    add_parser.add_argument("--dry-run", action="store_true", help="Perform checks without editing file")
    add_parser.add_argument("--skip-validation", action="store_true", help="Skip Alpaca asset & bar pre-flight check")

    # Subcommand: remove
    rem_parser = subparsers.add_parser("remove", help="Remove a symbol from the universe")
    rem_parser.add_argument("symbol", help="Stock ticker symbol to remove")

    args = parser.parse_args()

    if args.command == "list":
        return cmd_list(args)
    elif args.command == "validate":
        return cmd_validate(args)
    elif args.command == "add":
        return cmd_add(args)
    elif args.command == "remove":
        return cmd_remove(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
