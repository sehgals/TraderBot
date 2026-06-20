import json
import os
import sys
import urllib.error
import urllib.request

from traderbot.core_strategy_engine.engine import load_env


def main():
    load_env()

    base_url = os.environ.get("ALPACA_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    missing = [
        name
        for name, value in {
            "ALPACA_BASE_URL": base_url,
            "ALPACA_API_KEY": api_key,
            "ALPACA_SECRET_KEY": secret_key,
        }.items()
        if not value
    ]

    if missing:
        print(f"Missing required config: {', '.join(missing)}", file=sys.stderr)
        return 1

    request = urllib.request.Request(
        f"{base_url}/account",
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            account = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"Connection failed: HTTP {exc.code}", file=sys.stderr)
        print(body, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Connection failed: {exc.reason}", file=sys.stderr)
        return 1

    safe_fields = {
        "account_number": account.get("account_number"),
        "status": account.get("status"),
        "currency": account.get("currency"),
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
        "portfolio_value": account.get("portfolio_value"),
        "pattern_day_trader": account.get("pattern_day_trader"),
        "trading_blocked": account.get("trading_blocked"),
        "transfers_blocked": account.get("transfers_blocked"),
        "account_blocked": account.get("account_blocked"),
    }

    print("Alpaca paper account connection succeeded.")
    print(json.dumps(safe_fields, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
