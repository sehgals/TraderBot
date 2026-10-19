import datetime as dt
import os

from traderbot.data.tastytrade_process import TastytradeStreamProcess


class FakeStream:
    def __init__(self, symbols, start, timeframe):
        self.symbols = symbols
        self.start = start
        self.timeframe = timeframe

    def wait_ready(self, _timeout):
        return True

    def snapshot(self, symbols, _observed_at):
        return {symbol: {"quote": {}, "bars": []} for symbol in symbols}, 7

    def stop(self):
        pass


def test_stream_snapshots_cross_a_separate_process():
    process = TastytradeStreamProcess(
        ["MU", "WDC"], "2026-10-02T13:30:00Z", stream_factory=FakeStream
    ).start_background()
    try:
        assert process.pid != os.getpid()
        assert process.is_alive()
        assert process.wait_ready(2)
        data, events = process.snapshot(
            ["MU"], dt.datetime(2026, 10, 2, 14, tzinfo=dt.timezone.utc)
        )
        assert list(data) == ["MU"]
        assert events == 7
    finally:
        process.stop()
    assert not process.is_alive()
