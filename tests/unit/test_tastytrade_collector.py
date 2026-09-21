import datetime as dt
import json

from traderbot.data.tastytrade_collector import collect_tastytrade_comparison


class FakeAlpaca:
    def latest_quote(self, symbol):
        return {"ap": 101.0, "bp": 100.0}

    def stock_bars(self, symbol, start, end, timeframe):
        return [{"t": start, "c": 100.5, "v": 10}]


class FakeTastytrade:
    def stream_snapshot(self, symbols, start, timeframe, wait_seconds):
        return {
            symbol: {
                "quote": {"askPrice": 101.1, "bidPrice": 100.1},
                "bars": [{"t": start, "c": 100.6, "v": 11}],
            }
            for symbol in symbols
        }


def test_collector_keeps_source_data_separate(tmp_path):
    observed = dt.datetime(2026, 9, 20, 15, 0, tzinfo=dt.timezone.utc)
    result = collect_tastytrade_comparison(
        {"output_root": "market", "timeframe": "5Min", "lookback_hours": 1},
        ["mu", "MU", "wdc"],
        tmp_path,
        observed_at=observed,
        alpaca_client=FakeAlpaca(),
        tastytrade_client=FakeTastytrade(),
    )

    assert result["status"] == "tastytrade_collection_completed"
    assert result["collected_symbols"] == ["MU", "WDC"]
    collection_day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for source in ("alpaca", "tastytrade", "comparisons"):
        path = tmp_path / "market" / source / f"{collection_day}.jsonl"
        assert path.exists()
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [record["symbol"] for record in records] == ["MU", "WDC"]


def test_collector_skips_an_empty_symbol_list(tmp_path):
    result = collect_tastytrade_comparison({}, [], tmp_path)
    assert result == {
        "status": "tastytrade_collection_skipped",
        "reason": "no_symbols",
    }
