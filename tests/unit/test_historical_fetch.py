import datetime
import io
import json
import urllib.error
import urllib.parse
from email.message import Message
from unittest.mock import patch

from scripts import run_risk_control_backtest as backtest


UTC = datetime.timezone.utc


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def rate_limit_error(retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(
        "https://data.example.test",
        429,
        "Too Many Requests",
        headers,
        io.BytesIO(b"rate limited"),
    )


def test_month_windows_split_on_calendar_boundaries_across_year_end():
    start = datetime.datetime(2025, 12, 15, 12, tzinfo=UTC)
    end = datetime.datetime(2026, 2, 10, 18, tzinfo=UTC)

    windows = list(backtest.month_windows(start, end))

    assert windows == [
        (start, datetime.datetime(2026, 1, 1, tzinfo=UTC)),
        (
            datetime.datetime(2026, 1, 1, tzinfo=UTC),
            datetime.datetime(2026, 2, 1, tzinfo=UTC),
        ),
        (datetime.datetime(2026, 2, 1, tzinfo=UTC), end),
    ]


def test_429_uses_retry_after_then_succeeds():
    request = backtest.urllib.request.Request("https://data.example.test")
    with (
        patch.object(
            backtest.urllib.request,
            "urlopen",
            side_effect=[rate_limit_error(3), FakeResponse({"ok": True})],
        ) as urlopen,
        patch.object(backtest.time, "sleep") as sleep,
    ):
        result = backtest.request_json_with_backoff(request)

    assert result == {"ok": True}
    assert urlopen.call_count == 2
    sleep.assert_called_once_with(3.0)


def test_429_without_retry_after_uses_capped_exponential_backoff():
    request = backtest.urllib.request.Request("https://data.example.test")
    with (
        patch.object(
            backtest.urllib.request,
            "urlopen",
            side_effect=[
                rate_limit_error(),
                rate_limit_error(),
                FakeResponse({"ok": True}),
            ],
        ),
        patch.object(backtest.time, "sleep") as sleep,
    ):
        result = backtest.request_json_with_backoff(
            request,
            backoff_base_seconds=2,
            backoff_max_seconds=3,
        )

    assert result == {"ok": True}
    assert [call.args[0] for call in sleep.call_args_list] == [2, 3]


def test_fetch_bars_pages_each_month_and_deduplicates_boundary_timestamps():
    start = datetime.datetime(2026, 1, 15, tzinfo=UTC)
    end = datetime.datetime(2026, 3, 1, tzinfo=UTC)
    payloads = [
        {
            "bars": {"PANW": [{"t": "2026-01-15T14:30:00Z", "c": 100}]},
            "next_page_token": "jan-page-2",
        },
        {
            "bars": {"PANW": [{"t": "2026-02-01T00:00:00Z", "c": 101}]},
            "next_page_token": None,
        },
        {
            "bars": {"PANW": [
                {"t": "2026-02-01T00:00:00Z", "c": 101},
                {"t": "2026-02-20T14:30:00Z", "c": 102},
            ]},
            "next_page_token": None,
        },
    ]
    requests = []

    def fake_request(request, **_kwargs):
        requests.append(request)
        return payloads.pop(0)

    with (
        patch.object(backtest, "request_json_with_backoff", side_effect=fake_request),
        patch.object(backtest, "calculate_indicators", side_effect=lambda bars: bars),
        patch.object(backtest.time, "sleep"),
    ):
        result = backtest.fetch_bars(
            "PANW",
            "https://data.example.test/v2",
            {},
            start=start,
            end=end,
            request_pace_seconds=0.25,
        )

    assert [bar["t"] for bar in result] == [
        "2026-01-15T14:30:00Z",
        "2026-02-01T00:00:00Z",
        "2026-02-20T14:30:00Z",
    ]
    assert len(requests) == 3
    first = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0].full_url).query)
    second = urllib.parse.parse_qs(urllib.parse.urlparse(requests[1].full_url).query)
    third = urllib.parse.parse_qs(urllib.parse.urlparse(requests[2].full_url).query)
    assert first["start"] == ["2026-01-15T00:00:00Z"]
    assert first["end"] == ["2026-02-01T00:00:00Z"]
    assert second["page_token"] == ["jan-page-2"]
    assert third["start"] == ["2026-02-01T00:00:00Z"]
    assert third["end"] == ["2026-03-01T00:00:00Z"]


def test_parse_backtest_time_normalizes_to_utc():
    parsed = backtest.parse_backtest_time("2026-09-01T09:30:00-04:00")

    assert parsed == datetime.datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
