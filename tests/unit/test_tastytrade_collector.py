import datetime as dt
import json

from traderbot.data.tastytrade_collector import collect_tastytrade_comparison
from traderbot.data.market_session import session_open_utc


class FakeAlpaca:
    def calendar(self, start, end):
        return [{"date": start, "open": "09:30", "close": "16:00"}]

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


class FakeLiveStream:
    def wait_ready(self, _timeout):
        return True

    def snapshot(self, symbols, observed_at):
        stamp = "2026-09-21T13:30:00Z"
        return {
            symbol: {
                "quote": {"feed": "tastytrade_dxlink", "bid_price": 100, "ask_price": 101},
                "bars": [{"t": stamp, "c": 100}] if symbol == "MU" else [],
            }
            for symbol in symbols
        }, 12


def test_collector_keeps_source_data_separate(tmp_path):
    observed = dt.datetime(2026, 9, 21, 15, 0, tzinfo=dt.timezone.utc)
    result = collect_tastytrade_comparison(
        {"output_root": "market", "timeframe": "5Min"},
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
        if source != "comparisons":
            assert {record["start"] for record in records} == {"2026-09-21T13:30:00Z"}


def test_session_open_uses_calendar_and_eastern_daylight_saving():
    client = FakeAlpaca()
    summer = dt.datetime(2026, 9, 21, 15, tzinfo=dt.timezone.utc)
    winter = dt.datetime(2026, 1, 5, 15, tzinfo=dt.timezone.utc)

    assert session_open_utc(summer, client).isoformat() == "2026-09-21T13:30:00+00:00"
    assert session_open_utc(winter, client).isoformat() == "2026-01-05T14:30:00+00:00"


def test_live_collector_does_not_rank_symbols_without_a_candle(tmp_path):
    observed = dt.datetime(2026, 9, 21, 15, tzinfo=dt.timezone.utc)
    result = collect_tastytrade_comparison(
        {"output_root": "market"}, ["MU", "WDC"], tmp_path,
        observed_at=observed, alpaca_client=FakeAlpaca(),
        tastytrade_stream=FakeLiveStream(),
    )

    assert result["collected_symbols"] == ["MU"]
    assert result["no_candle_symbols"] == ["WDC"]
    assert result["stream_events"] == 12
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    row = json.loads((tmp_path / "market" / "comparisons" / f"{day}.jsonl").read_text())
    assert row["schema_version"] == 3
    assert row["collection_mode"] == "continuous_stream"


def test_collector_skips_an_empty_symbol_list(tmp_path):
    result = collect_tastytrade_comparison({}, [], tmp_path)
    assert result == {
        "status": "tastytrade_collection_skipped",
        "reason": "no_symbols",
    }
