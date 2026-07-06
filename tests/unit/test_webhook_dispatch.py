"""Unit tests for webhook submission-type normalization (death/diary additions)."""

from api.routes.webhook import _normalize_submission_type, _normalize_world_type


class TestNormalizeSubmissionType:
    def test_drop_aliases(self):
        assert _normalize_submission_type("other") == "drop"
        assert _normalize_submission_type("npc") == "drop"
        assert _normalize_submission_type("drop") == "drop"

    def test_personal_best_aliases(self):
        assert _normalize_submission_type("kill_time") == "personal_best"
        assert _normalize_submission_type("npc_kill") == "personal_best"

    def test_experience_aliases(self):
        assert _normalize_submission_type("experience_update") == "experience"
        assert _normalize_submission_type("experience_milestone") == "experience"
        assert _normalize_submission_type("level_up") == "experience"

    def test_quest_aliases(self):
        assert _normalize_submission_type("quest_completion") == "quest"
        assert _normalize_submission_type("quest") == "quest"

    def test_death_aliases(self):
        assert _normalize_submission_type("death") == "death"
        assert _normalize_submission_type("player_death") == "death"
        assert _normalize_submission_type("PLAYER_DEATH") == "death"
        assert _normalize_submission_type(" Death ") == "death"

    def test_diary_aliases(self):
        assert _normalize_submission_type("diary") == "diary"
        assert _normalize_submission_type("achievement_diary") == "diary"
        assert _normalize_submission_type("diary_completion") == "diary"
        assert _normalize_submission_type("Achievement_Diary") == "diary"

    def test_passthrough_and_empty(self):
        assert _normalize_submission_type("pet") == "pet"
        assert _normalize_submission_type("adventure_log") == "adventure_log"
        assert _normalize_submission_type(None) == ""
        assert _normalize_submission_type("") == ""


class TestNormalizeWorldType:
    def test_defaults_to_main(self):
        assert _normalize_world_type(None) == "main"
        assert _normalize_world_type("") == "main"
        assert _normalize_world_type("  ") == "main"

    def test_lowercases_and_strips(self):
        assert _normalize_world_type(" Seasonal ") == "seasonal"
        assert _normalize_world_type("MAIN") == "main"


class TestConfigRegistryDeathDiaryKeys:
    def test_notify_keys_registered(self):
        from web_api import config_registry as reg

        deaths = reg.get_config_field("notify_deaths")
        assert deaths is not None
        assert deaths["type"] == "boolean"
        assert deaths["default"] is False
        assert deaths.get("seasonal") is True

        diaries = reg.get_config_field("notify_diaries")
        assert diaries is not None
        assert diaries["type"] == "boolean"
        assert diaries["default"] is False
        assert diaries.get("seasonal") is True

    def test_seasonal_mirrors_resolve(self):
        from web_api import config_registry as reg

        assert reg.get_config_field("seasonal_notify_deaths")["key"] == "notify_deaths"
        assert reg.get_config_field("seasonal_notify_diaries")["key"] == "notify_diaries"

    def test_channel_keys_registered(self):
        from web_api import config_registry as reg

        for key in ("channel_id_to_post_deaths", "channel_id_to_post_diaries"):
            field = reg.get_config_field(key)
            assert field is not None
            assert field["type"] == "channel"
            assert field["default"] is None
