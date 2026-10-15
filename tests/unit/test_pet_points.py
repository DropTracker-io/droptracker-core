"""Clan points for pets (data/submissions/pet_points.py), 2026-10-15.

The "New pets" rule used to pay on every duplicate, because pet.py ran the
award pass for duplicates too and the engine never knew which kind it had.
Pinned here:

* a member earns a pet's new-pet points once per group; later copies pay only
  when the group's duplicate setting covers that pet (never by default);
* a duplicate can pay its own flat amount, still through boosts and limits;
* the previously-owned sweep pays only unpaid pets, never writes on a
  preview, and skips pets whose award comes out at zero.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import data.submissions.common as common
import data.submissions.pet_points as pp
import data.submissions.point_awards as pa
from tests.unit.test_point_awards_boost_split import (
    GROUP_ID, NOW, RECEIVER_ID, FakeSession, _patch_pipeline,
)


def _row(value="", long_value=None):
    return SimpleNamespace(config_value=value, long_value=long_value)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class TestSettings:
    def test_default_is_no_duplicates(self):
        settings = pp.parse_settings({})
        assert settings.duplicates == "none"
        assert settings.duplicate_award is None
        assert settings.prior_owned is False
        assert not settings.duplicate_pays("Olmlet")

    def test_all_mode_pays_any_pet(self):
        settings = pp.parse_settings({pp.DUPLICATES_MODE_KEY: _row("all")})
        assert settings.duplicate_pays("Chompy chick")

    def test_selected_mode_pays_listed_pets_only(self):
        settings = pp.parse_settings({
            pp.DUPLICATES_MODE_KEY: _row("selected"),
            pp.DUPLICATE_PETS_KEY: _row("", json.dumps(["Olmlet", "Lil' zik"])),
        })
        assert settings.duplicate_pays("olmlet")
        assert settings.duplicate_pays("  LIL' ZIK ")
        assert not settings.duplicate_pays("Chompy chick")

    def test_list_is_ignored_outside_selected_mode(self):
        settings = pp.parse_settings({
            pp.DUPLICATES_MODE_KEY: _row("none"),
            pp.DUPLICATE_PETS_KEY: _row("", json.dumps(["Olmlet"])),
        })
        assert not settings.duplicate_pays("Olmlet")

    def test_unreadable_values_fall_back_safely(self):
        settings = pp.parse_settings({
            pp.DUPLICATES_MODE_KEY: _row("sometimes"),
            pp.DUPLICATE_PETS_KEY: _row("", "{not json"),
            pp.DUPLICATE_AWARD_KEY: _row("lots"),
            pp.PRIOR_OWNED_KEY: _row("yes"),
        })
        assert settings == pp.PetPointSettings()

    def test_custom_duplicate_award(self):
        settings = pp.parse_settings({pp.DUPLICATE_AWARD_KEY: _row("7")})
        assert settings.duplicate_award == 7
        assert pp.parse_settings({pp.DUPLICATE_AWARD_KEY: _row("")}).duplicate_award is None

    def test_normalize_canonicalizes_and_reports_unknown(self):
        known, unknown = pp.normalize_duplicate_pets(
            ["olmlet", "OLMLET", "Lil' zik", "Not a pet", "", 5])
        assert known == ["Lil' zik", "Olmlet"]
        assert unknown == ["Not a pet"]


# --------------------------------------------------------------------------- #
# Live pet decision
# --------------------------------------------------------------------------- #
class TestPlanLivePet:
    def _plan(self, monkeypatch, *, paid, settings):
        monkeypatch.setattr(pp, "new_pet_award_exists", lambda *a, **k: paid)
        return pp.plan_live_pet(None, GROUP_ID, RECEIVER_ID, "Olmlet", 20851,
                                settings=settings)

    def test_first_copy_pays_as_new_even_with_duplicates_off(self, monkeypatch):
        plan = self._plan(monkeypatch, paid=False, settings=pp.PetPointSettings())
        assert plan.award and not plan.duplicate

    def test_duplicate_pays_nothing_by_default(self, monkeypatch):
        plan = self._plan(monkeypatch, paid=True, settings=pp.PetPointSettings())
        assert not plan.award and plan.duplicate

    def test_selected_duplicate_pays_its_own_amount(self, monkeypatch):
        settings = pp.PetPointSettings(duplicates="selected", duplicate_pets=("Olmlet",),
                                       duplicate_award=3)
        plan = self._plan(monkeypatch, paid=True, settings=settings)
        assert plan.award and plan.duplicate and plan.duplicate_award == 3

    def test_unselected_duplicate_pays_nothing(self, monkeypatch):
        settings = pp.PetPointSettings(duplicates="selected", duplicate_pets=("Nexling",))
        assert not self._plan(monkeypatch, paid=True, settings=settings).award


# --------------------------------------------------------------------------- #
# Engine options used by pets
# --------------------------------------------------------------------------- #
def _pet_engine(monkeypatch, *, pet_rule="10,1"):
    _patch_pipeline(monkeypatch, sharing=False)

    async def pet_config(group_id, external_session=None):
        return {"pet": pet_rule}

    writes = []

    async def fake_award(reason, group_id, player_id, points, entry_id=None,
                         external_session=None, entry_type=None):
        writes.append({"reason": reason, "points": int(points), "entry_id": entry_id,
                       "entry_type": entry_type})
        return int(points)

    boosts = []

    async def doubling_boost(reason, group_id, player_id, points, **kw):
        boosts.append(points)
        return points * 2

    monkeypatch.setattr(pa, "get_point_config", pet_config)
    monkeypatch.setattr(pa, "award_player_points", fake_award)
    monkeypatch.setattr(pa, "modify_for_event", doubling_boost)
    return writes, boosts


async def _award(**kwargs):
    return await pa._check_and_award_points(
        "pet", GROUP_ID, RECEIVER_ID, 1, item_id=20851, entry_id=77,
        submission_timestamp=NOW, external_session=FakeSession([]), **kwargs,
    )


class TestEngineOptions:
    async def test_plain_pet_unchanged(self, monkeypatch):
        writes, _ = _pet_engine(monkeypatch)
        await _award()
        assert writes == [{"reason": "pet", "points": 20, "entry_id": 77, "entry_type": None}]

    async def test_duplicate_uses_its_amount_label_and_type(self, monkeypatch):
        writes, boosts = _pet_engine(monkeypatch)
        await _award(ledger_reason=pp.DUPLICATE_REASON, entry_type=pp.DUPLICATE_ENTRY_TYPE,
                     fixed_award=3)
        # The flat amount replaces the rule, then the live boost still applies.
        assert boosts == [3]
        assert writes == [{"reason": "pet (duplicate)", "points": 6, "entry_id": 77,
                           "entry_type": 20}]

    async def test_dry_run_writes_nothing(self, monkeypatch):
        writes, _ = _pet_engine(monkeypatch)
        result = await _award(dry_run=True, skip_boosts=True)
        assert writes == []
        assert result["receiver_points_awarded"] == 10

    async def test_skip_boosts(self, monkeypatch):
        writes, boosts = _pet_engine(monkeypatch)
        await _award(skip_boosts=True)
        assert boosts == []
        assert writes[0]["points"] == 10


# --------------------------------------------------------------------------- #
# Previously-owned sweep
# --------------------------------------------------------------------------- #
def _sweep_env(monkeypatch, *, owned, paid, each=None):
    monkeypatch.setattr(common, "check_group_point_system_active", lambda g, s=None: True)
    monkeypatch.setattr(pp, "_member_player_ids", lambda s, g: set(owned))
    monkeypatch.setattr(pp, "_owned_pets", lambda s, ids: {p: owned[p] for p in ids})
    monkeypatch.setattr(pp, "_paid_pets", lambda s, g, ids: set(paid))
    each = each or {}

    async def quote(reason, group_id, player_id, value, item_id=None, **kw):
        assert kw.get("dry_run") and kw.get("skip_boosts")
        return {"receiver_points_awarded": each.get(item_id, 10)}

    writes = []

    async def fake_award(reason, group_id, player_id, points, entry_id=None, *,
                         external_session=None, entry_type=None):
        writes.append((player_id, entry_id, points, reason, entry_type))
        return points

    monkeypatch.setattr(pa, "check_and_award_points", quote)
    monkeypatch.setattr(pa, "award_player_points", fake_award)
    return writes


class TestPriorOwnedSweep:
    OWNED = {1: {"Olmlet": 20851, "Chompy chick": 13071}, 2: {"Olmlet": 20851}}

    async def test_pays_only_unpaid_pets(self, monkeypatch):
        writes = _sweep_env(monkeypatch, owned=self.OWNED, paid={(1, "Olmlet")})
        result = await pp.sweep_prior_owned_pets(None, GROUP_ID, apply=True)
        assert sorted(writes) == [
            (1, 13071, 10, pp.PRIOR_OWNED_REASON, pp.PRIOR_OWNED_ENTRY_TYPE),
            (2, 20851, 10, pp.PRIOR_OWNED_REASON, pp.PRIOR_OWNED_ENTRY_TYPE),
        ]
        assert (result.members, result.awards, result.points) == (2, 2, 20)

    async def test_preview_writes_nothing(self, monkeypatch):
        writes = _sweep_env(monkeypatch, owned=self.OWNED, paid=set())
        result = await pp.sweep_prior_owned_pets(None, GROUP_ID, apply=False)
        assert writes == []
        assert (result.members, result.awards, result.points) == (2, 3, 30)
        assert result.by_pet[0] == {"pet": "Olmlet", "item_id": 20851, "members": 2,
                                    "points_each": 10}

    async def test_zero_value_pets_are_skipped(self, monkeypatch):
        # e.g. Chompy chick on the group's exclude list.
        writes = _sweep_env(monkeypatch, owned=self.OWNED, paid=set(), each={13071: 0})
        result = await pp.sweep_prior_owned_pets(None, GROUP_ID, apply=True)
        assert all(w[1] == 20851 for w in writes)
        assert [r["pet"] for r in result.by_pet] == ["Olmlet"]

    async def test_inactive_point_system_does_nothing(self, monkeypatch):
        writes = _sweep_env(monkeypatch, owned=self.OWNED, paid=set())
        monkeypatch.setattr(common, "check_group_point_system_active", lambda g, s=None: False)
        result = await pp.sweep_prior_owned_pets(None, GROUP_ID, apply=True)
        assert writes == [] and result.awards == 0


# --------------------------------------------------------------------------- #
# Settings PUT (web_api/routes/points.py)
# --------------------------------------------------------------------------- #
class TestSettingsRoute:
    def _apply(self, monkeypatch, body):
        import web_api.routes.points as routes

        written = {}
        monkeypatch.setattr(pp, "write_settings", lambda s, g, **kw: written.update(kw))
        changed = {}
        routes._apply_pet_behavior(None, GROUP_ID, body, changed)
        return written, changed

    def test_valid_settings_are_canonicalized(self, monkeypatch):
        written, changed = self._apply(monkeypatch, {
            "pet_duplicates": "Selected",
            "pet_duplicate_pets": ["olmlet", "Lil' zik"],
            "pet_duplicate_award": 5,
        })
        assert written == {"duplicates": "selected",
                           "duplicate_pets": ["Lil' zik", "Olmlet"],
                           "duplicate_award": 5}
        assert changed["behavior"]["pet_duplicates"] == "selected"

    def test_blank_award_means_same_as_new(self, monkeypatch):
        written, _ = self._apply(monkeypatch, {"pet_duplicate_award": None})
        assert written == {"duplicate_award": None}

    def test_prior_owned_is_not_accepted_here(self, monkeypatch):
        written, changed = self._apply(monkeypatch, {"pet_prior_owned": True})
        assert written == {} and changed == {}

    def test_bad_values_are_rejected(self, monkeypatch):
        import pytest

        for body in ({"pet_duplicates": "sometimes"},
                     {"pet_duplicate_pets": ["Not a pet"]},
                     {"pet_duplicate_pets": "Olmlet"},
                     {"pet_duplicate_award": -1}):
            with pytest.raises(Exception):
                self._apply(monkeypatch, body)
