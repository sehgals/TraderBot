import json

from traderbot.dashboard.feed_quality import feed_quality_snapshot


def test_feed_quality_scores_each_ticker(tmp_path):
    directory = tmp_path / "runtime/market_data/comparisons"
    directory.mkdir(parents=True)
    rows = []
    for index in range(20):
        rows.append({
            "symbol": "MU", "observed_at": f"2026-09-18T15:{index:02d}:00Z",
            "primary_source": "alpaca", "secondary_source": "tastytrade",
            "midpoint_difference_bps": 0.5,
            "alpaca_midpoint": 100, "tastytrade_midpoint": 100.005,
            "alpaca_bar_count": 95, "tastytrade_bar_count": 96,
            "alpaca_spread_percent": 0.03, "tastytrade_spread_percent": 0.02,
        })
    rows.append({"symbol": "WDC", "observed_at": "2026-09-18T15:00:00Z",
                 "primary_source": "alpaca", "secondary_source": "other"})
    (directory / "2026-09-18.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )

    result = feed_quality_snapshot(tmp_path)

    assert len(result) == 1
    assert result[0]["symbol"] == "MU"
    assert result[0]["score"] >= 90
    assert result[0]["preferred_source"] == "tastytrade"
    assert result[0]["samples"] == 20


def test_feed_quality_ignores_legacy_records_after_corrected_schema_arrives(tmp_path):
    directory = tmp_path / "runtime/market_data/comparisons"
    directory.mkdir(parents=True)
    rows = [
        {
            "schema_version": 1, "symbol": "MU", "observed_at": "2026-09-18T14:00:00Z",
            "primary_source": "alpaca", "secondary_source": "tastytrade",
            "alpaca_bar_count": 1, "tastytrade_bar_count": 80,
        },
        {
            "schema_version": 2, "symbol": "MU", "observed_at": "2026-09-18T15:00:00Z",
            "primary_source": "alpaca", "secondary_source": "tastytrade",
            "alpaca_bar_count_in_window": 12, "tastytrade_bar_count_in_window": 10,
            "alpaca_midpoint": 100, "tastytrade_midpoint": 100,
            "midpoint_difference_bps": 0,
        },
    ]
    (directory / "2026-09-18.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )

    result = feed_quality_snapshot(tmp_path)

    assert result[0]["samples"] == 1
    assert result[0]["preferred_source"] == "alpaca"
