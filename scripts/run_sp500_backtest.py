import html
import pathlib
import re
import subprocess
import sys
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "runtime" / "reports" / "backtests"
SYMBOLS_PATH = OUT_DIR / "sp500_symbols_2026-07-03.txt"
REPORT_PATH = OUT_DIR / "sp500_backtest_2025-07-03_to_2026-07-03_5Min.txt"


def fetch_sp500_symbols():
    request = urllib.request.Request(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 TraderBot backtest"},
    )
    page = urllib.request.urlopen(request, timeout=30).read().decode("utf-8", "ignore")
    table_match = re.search(
        r'<table[^>]*id="constituents"[^>]*>(.*?)</table>',
        page,
        re.S,
    )
    if not table_match:
        raise RuntimeError("Could not find S&P 500 constituents table")

    symbols = []
    for row in re.findall(r"<tr>(.*?)</tr>", table_match.group(1), re.S)[1:]:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if not cells:
            continue
        symbol = html.unescape(re.sub(r"<[^>]+>", "", cells[0])).strip()
        symbol = symbol.replace("-", ".").upper()
        if symbol:
            symbols.append(symbol)
    return sorted(set(symbols))


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    symbols = fetch_sp500_symbols()
    SYMBOLS_PATH.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    print(f"Fetched {len(symbols)} S&P 500 symbols")

    command = [
        sys.executable,
        "-m",
        "traderbot.cli.backtest",
        "--start",
        "2025-07-03T00:00:00Z",
        "--end",
        "2026-07-03T00:00:00Z",
        "--timeframe",
        "5Min",
        "--symbols",
        *symbols,
    ]
    with REPORT_PATH.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        for line in process.stdout:
            print(line, end="")
            output.write(line)
        return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
