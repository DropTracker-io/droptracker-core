"""Unit tests for experience_update snapshot parsing (plugin XP sync)."""

import json

from data.submissions.experience import (
    _is_snapshot_submission,
    _parse_skills_data,
    _parse_snapshot_skills,
)


class TestIsSnapshotSubmission:
    def test_detects_experience_update_type(self):
        assert _is_snapshot_submission({"type": "experience_update"})
        assert _is_snapshot_submission({"type": " Experience_Update "})

    def test_detects_skills_data_field(self):
        assert _is_snapshot_submission({"skills_data": '{"magic": [13034431, 99]}'})

    def test_level_up_is_not_snapshot(self):
        assert not _is_snapshot_submission({"type": "level_up", "skills_leveled": "Magic"})
        assert not _is_snapshot_submission({})


class TestParseSnapshotSkills:
    def test_parses_json_string_of_pairs(self):
        payload = {"skills_data": json.dumps({
            "magic": [13034431, 99],
            "attack": [737627, 60],
        })}
        skills = {s["skill_key"]: s for s in _parse_snapshot_skills(payload)}
        assert skills["magic"]["xp_total"] == 13034431
        assert skills["magic"]["new_level"] == 99
        assert skills["attack"]["xp_total"] == 737627

    def test_ignores_unknown_skills_and_bad_values(self):
        payload = {"skills_data": json.dumps({
            "combat": [100, 100],       # not a PlayerExperience column
            "notaskill": [5, 5],
            "magic": ["oops", "bad"],   # coerced to 0
        })}
        skills = _parse_snapshot_skills(payload)
        assert [s["skill_key"] for s in skills] == ["magic"]
        assert skills[0]["xp_total"] == 0

    def test_accepts_dict_and_scalar_values(self):
        payload = {"skills_data": {"magic": {"xp": 200, "level": 3}, "attack": 500}}
        skills = {s["skill_key"]: s for s in _parse_snapshot_skills(payload)}
        assert skills["magic"]["xp_total"] == 200
        assert skills["magic"]["new_level"] == 3
        assert skills["attack"]["xp_total"] == 500

    def test_empty_or_invalid_inputs(self):
        assert _parse_snapshot_skills({}) == []
        assert _parse_snapshot_skills({"skills_data": ""}) == []
        assert _parse_snapshot_skills({"skills_data": "not json"}) == []
        assert _parse_snapshot_skills({"skills_data": "[1, 2]"}) == []


class TestParseSkillsDataFallback:
    def test_falls_back_to_skills_trained_when_leveled_empty(self):
        # XP milestone submissions send skills_leveled="" with the affected
        # skills only listed in skills_trained.
        payload = {
            "skills_leveled": "",
            "skills_trained": "Magic",
            "magic_xp_total": "14000000",
        }
        skills = _parse_skills_data(payload)
        assert len(skills) == 1
        assert skills[0]["skill_key"] == "magic"
        assert skills[0]["xp_total"] == 14000000
