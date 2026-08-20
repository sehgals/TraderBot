from scripts.migrate_managed_reentry import migration_changes


def test_migration_enforces_managed_lifecycle_without_changing_other_risk_settings():
    config = {
        "dynamic_entry_enabled": True,
        "reentry_enabled": False,
        "dynamic_reentry_enabled": False,
        "risk_profile": "speculative",
    }

    changes = migration_changes(config)

    assert changes == {
        "dynamic_entry_enabled": {"from": True, "to": False},
        "reentry_enabled": {"from": False, "to": True},
        "dynamic_reentry_enabled": {"from": False, "to": True},
    }
    assert config["risk_profile"] == "speculative"
