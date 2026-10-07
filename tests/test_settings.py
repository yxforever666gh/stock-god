import json

import pytest

from stock_god.settings import DEFAULTS, SettingsConflict, SettingsStore


def initialize(database):
    with database.transaction() as con:
        con.execute("INSERT INTO research_settings VALUES ('research1',?,1)", ('{"preserved":true}',))
        con.execute(
            "INSERT INTO research_settings VALUES ('research2',?,1)",
            (json.dumps({"research2AutoEnabled": True, "minuteProviderOrder": ["sina", "tencent"]}),),
        )
        con.execute(
            "INSERT INTO ai_config(id,owner,name,base_url,api_key,model_name,api_protocol,sort,disabled) "
            "VALUES (1,'research1','historical','https://old.invalid','old-secret','old','chat_completions',1,0)"
        )
        con.execute(
            "INSERT INTO ai_config(id,owner,name,base_url,api_key,model_name,api_protocol,sort,disabled) "
            "VALUES (2,'research2','current','https://new.invalid','new-secret','new','chat_completions',1,0)"
        )
        con.execute(
            "INSERT INTO settings(id,dark_theme,refresh_interval,update_basic_info_on_start) VALUES (1,0,5,0)"
        )
    return SettingsStore(database)


def test_deep_snapshot_and_compare_and_swap_preserve_other_owners(core_database):
    store = initialize(core_database)
    first = store.load()
    draft = first.payload()
    draft["config"]["minuteProviderOrder"].append("private")
    draft["config"]["predictionAutoEnabled"] = False
    saved = store.save(draft["revision"], draft["config"], draft["aiConfigs"])
    assert saved.revision == 2
    assert first.config["predictionAutoEnabled"] is True
    assert first.config["minuteProviderOrder"] == ["sina", "tencent"]
    with pytest.raises(SettingsConflict):
        store.save(first.revision, first.config, first.models)
    with core_database.connection() as con:
        assert (
            con.execute("SELECT config_json FROM research_settings WHERE center='research1'").fetchone()[0]
            == '{"preserved":true}'
        )
        assert con.execute("SELECT api_key,archived_at FROM ai_config WHERE id=1").fetchone()[:] == (
            "old-secret",
            None,
        )
        raw = con.execute("SELECT config_json FROM research_settings WHERE center='research2'").fetchone()[0]
        assert "predictionAutoEnabled" not in raw
        assert json.loads(raw)["research2AutoEnabled"] is False


def test_meoz_key_snapshot_storage_and_redaction(core_database):
    from stock_god.audit import redact_text

    store = initialize(core_database)
    first = store.load()
    assert first.config["meozApiKey"] == ""
    draft = first.payload()
    draft["config"]["meozApiKey"] = "  fixture-meoz-key  "
    draft["config"]["predictionEmailSlots"] = ["base43", "09:30"]
    saved = store.save(first.revision, draft["config"], draft["aiConfigs"])
    assert first.config["meozApiKey"] == ""
    assert saved.config["meozApiKey"] == "fixture-meoz-key"
    assert store.load().config["predictionEmailSlots"] == ["09:30", "base43"]
    with pytest.raises(SettingsConflict):
        store.save(first.revision, draft["config"], draft["aiConfigs"])
    redacted, _ = redact_text(json.dumps(saved.config))
    assert "fixture-meoz-key" not in redacted
    assert json.loads(redacted)["meozApiKey"] == "[REDACTED]"


def test_foreign_model_rolls_back_revision_and_deletion_archives(core_database):
    store = initialize(core_database)
    first = store.load()
    with pytest.raises(ValueError, match="does not belong"):
        store.save(first.revision, first.config, [{"ID": 1, "name": "steal"}])
    assert store.load().revision == 1
    saved = store.save(1, first.config, [])
    assert saved.models == []
    with core_database.connection() as con:
        assert con.execute("SELECT archived_at FROM ai_config WHERE id=2").fetchone()[0]
        assert con.execute("SELECT COUNT(*) FROM ai_config").fetchone()[0] == 2


def test_current_configuration_validation_and_new_model_id(core_database):
    store = initialize(core_database)
    config = DEFAULTS.copy()
    config["research1Enabled"] = True
    with pytest.raises(ValueError):
        store.save(1, config, [])
    config = DEFAULTS.copy()
    config["privateMinuteEnabled"] = True
    with pytest.raises(ValueError, match="URL"):
        store.save(1, config, [])
    saved = store.save(
        1,
        DEFAULTS,
        [
            {
                "ID": 0,
                "name": "added",
                "modelName": "model",
                "baseUrl": "https://example.invalid",
                "apiKey": "fixture",
            }
        ],
    )
    assert saved.models[0]["ID"] > 2
    assert saved.models[0]["timeOut"] == 300
    assert saved.models[0]["apiProtocol"] == "chat_completions"


def test_global_changes_cannot_write_prediction_settings(core_database):
    store = initialize(core_database)
    with pytest.raises(ValueError):
        store.save_global({"predictionAutoEnabled": False})
    result = store.save_global({"darkTheme": True})
    assert result == {"darkTheme": True, "refreshInterval": 5, "updateBasicInfoOnStart": False}
    assert store.load().revision == 1
