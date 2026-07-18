from traderbot.core_strategy_engine.engine import AlpacaClient


def test_fills_paginates_bare_list_activity_responses():
    client = object.__new__(AlpacaClient)
    first_page = [{"id": f"activity-{index}"} for index in range(100)]
    second_page = [{"id": "activity-100"}]
    requested_paths = []

    def trading(_method, path, _payload=None):
        requested_paths.append(path)
        return first_page if len(requested_paths) == 1 else second_page

    client.trading = trading

    fills = client.fills("2026-01-01T00:00:00Z", "2026-07-17T00:00:00Z")

    assert len(fills) == 101
    assert "page_token=activity-99" in requested_paths[1]
