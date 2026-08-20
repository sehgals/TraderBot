from traderbot.monitoring.supervisor import managed_strategy_config


def test_promoted_candidate_enables_reentry_and_disables_fresh_entry():
    config = managed_strategy_config(
        "ALMU",
        {
            "dynamic_entry_enabled": True,
            "reentry_enabled": False,
            "dynamic_reentry_enabled": False,
            "risk_profile": "speculative",
        },
    )

    assert config["dynamic_entry_enabled"] is False
    assert config["reentry_enabled"] is True
    assert config["dynamic_reentry_enabled"] is True
    assert config["risk_profile"] == "speculative"
