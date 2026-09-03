from traderbot.backtester.reentry_backtest import isolate_entry_model_configs


def test_model_isolation_enables_exactly_one_family_without_mutating_source():
    source = {"PANW": {"entry_models": {"pullback": {"enabled": True}}}}
    result = isolate_entry_model_configs(source, "relative_strength")
    enabled = [
        name for name, settings in result["PANW"]["entry_models"].items()
        if settings["enabled"]
    ]
    assert enabled == ["relative_strength"]
    assert source["PANW"]["entry_models"]["pullback"]["enabled"] is True
