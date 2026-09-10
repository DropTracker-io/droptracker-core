"""Event clan-point awards (web114a) — the pure scoring core.

``services/event_point_awards.py`` keeps its rules stdlib-only above the DB
divider, so it loads here by file path (the ``test_event_prizes`` idiom) and
the conftest's stubbed ``db``/``services`` packages never get involved.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "event_point_awards.py",
)
_spec = importlib.util.spec_from_file_location("_event_point_awards_under_test", _MODULE_PATH)
epa = importlib.util.module_from_spec(_spec)
sys.modules["_event_point_awards_under_test"] = epa
_spec.loader.exec_module(epa)


def _cfg(**over):
    cfg = epa.effective_points_config(None)
    part = over.pop("participation", None)
    cfg.update(over)
    if part:
        cfg["participation"].update(part)
    return cfg


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class TestEffectiveConfig:
    def test_defaults_when_unset(self):
        cfg = epa.effective_points_config(None)
        assert cfg == {
            "enabled": False, "award_mode": "auto", "placement": [],
            "placement_active_only": True,
            "participation": {"flat": 0, "per_hour": 0, "min_hours": 0, "max": 0},
        }

    def test_defaults_are_a_copy(self):
        cfg = epa.effective_points_config(None)
        cfg["participation"]["flat"] = 99
        assert epa.effective_points_config(None)["participation"]["flat"] == 0

    def test_corrupt_json_falls_back(self):
        assert epa.effective_points_config("{not json")["enabled"] is False
        assert epa.effective_points_config("[1, 2]")["placement"] == []

    def test_reads_stored_values(self):
        raw = json.dumps({"enabled": True, "award_mode": "review",
                          "placement": [100, 50, 25], "placement_active_only": False,
                          "participation": {"flat": 5, "per_hour": 2.5,
                                            "min_hours": 1, "max": 200}})
        cfg = epa.effective_points_config(raw)
        assert cfg["enabled"] and cfg["award_mode"] == "review"
        assert cfg["placement"] == [100, 50, 25]
        assert cfg["placement_active_only"] is False
        assert cfg["participation"] == {"flat": 5, "per_hour": 2.5,
                                        "min_hours": 1.0, "max": 200}

    def test_out_of_range_values_drop_to_defaults(self):
        cfg = epa.effective_points_config({
            "award_mode": "sometimes", "placement": [10, -1],
            "participation": {"flat": 1.5, "per_hour": -2, "max": True},
        })
        assert cfg["award_mode"] == "auto"
        assert cfg["placement"] == []
        assert cfg["participation"] == {"flat": 0, "per_hour": 0, "min_hours": 0, "max": 0}

    def test_trailing_zero_places_trimmed(self):
        assert epa.effective_points_config({"placement": [10, 0, 5, 0, 0]})["placement"] == [10, 0, 5]


class TestNormalizeInput:
    def test_partial_payload(self):
        assert epa.normalize_points_input({"enabled": True}) == {"enabled": True}

    def test_full_payload(self):
        out = epa.normalize_points_input({
            "enabled": True, "award_mode": "review", "placement": [3, 2, 1],
            "placement_active_only": False,
            "participation": {"flat": 1, "per_hour": 0.5, "min_hours": 2, "max": 10},
        })
        assert out["participation"] == {"flat": 1, "per_hour": 0.5, "min_hours": 2.0, "max": 10}

    @pytest.mark.parametrize("body", [
        "nope",
        {"enabled": "yes"},
        {"award_mode": "whenever"},
        {"placement": [1] * (11)},
        {"placement": [100, "50"]},
        {"placement": [2_000_000]},
        {"placement_active_only": 1},
        {"participation": []},
        {"participation": {"flat": 1.5}},
        {"participation": {"per_hour": 20_000}},
        {"participation": {"min_hours": -1}},
        {"participation": {"max": True}},
    ])
    def test_rejects_invalid(self, body):
        with pytest.raises(epa.PointsConfigError):
            epa.normalize_points_input(body)

    def test_error_message_names_the_field(self):
        with pytest.raises(epa.PointsConfigError, match="participation.per_hour"):
            epa.normalize_points_input({"participation": {"per_hour": "lots"}})

    def test_merge_is_keywise_for_participation(self):
        current = json.dumps({"enabled": True, "participation": {"flat": 5, "per_hour": 2}})
        merged = epa.merge_points_config(current, {"participation": {"per_hour": 3}})
        assert merged["enabled"] is True
        assert merged["participation"]["flat"] == 5
        assert merged["participation"]["per_hour"] == 3


class TestConfigPredicates:
    def test_pays_anything(self):
        assert not epa.pays_anything(_cfg(enabled=False, placement=[10]))
        assert not epa.pays_anything(_cfg(enabled=True))
        assert epa.pays_anything(_cfg(enabled=True, placement=[0, 5]))
        assert epa.pays_anything(_cfg(enabled=True, participation={"flat": 1}))
        assert epa.pays_anything(_cfg(enabled=True, participation={"per_hour": 0.5}))

    def test_needs_ehe_pricing(self):
        assert epa.needs_ehe_pricing(_cfg(participation={"per_hour": 1}))
        # A floor only matters when it gates something.
        assert epa.needs_ehe_pricing(_cfg(participation={"min_hours": 1, "flat": 5}))
        assert not epa.needs_ehe_pricing(_cfg(participation={"min_hours": 1}))
        assert not epa.needs_ehe_pricing(_cfg(placement=[10], participation={"flat": 5}))
        # SOTW/BOTW never price hours.
        assert not epa.needs_ehe_pricing(_cfg(participation={"per_hour": 1}),
                                         ehe_supported=False)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class TestCompetitionPlaces:
    def test_distinct(self):
        assert epa.competition_places([("a", 30), ("b", 20), ("c", 10)]) == [
            ("a", 1), ("b", 2), ("c", 3)]

    def test_ties_share_and_skip(self):
        assert epa.competition_places([("a", 30), ("b", 30), ("c", 10), ("d", 10), ("e", 5)]) == [
            ("a", 1), ("b", 1), ("c", 3), ("d", 3), ("e", 5)]

    def test_tuple_keys(self):
        assert epa.competition_places([(1, (True, 20)), (2, (True, 20)), (3, (False, 20))]) == [
            (1, 1), (2, 1), (3, 3)]

    def test_empty(self):
        assert epa.competition_places([]) == []


class TestPlacementAmount:
    def test_paid_places(self):
        cfg = _cfg(placement=[100, 50, 25])
        assert [epa.placement_amount(cfg, p) for p in (1, 2, 3, 4)] == [100, 50, 25, 0]

    def test_unplaced(self):
        cfg = _cfg(placement=[100])
        assert epa.placement_amount(cfg, None) == 0
        assert epa.placement_amount(cfg, 0) == 0


class TestParticipationAmount:
    def test_nothing_without_taking_part(self):
        cfg = _cfg(participation={"flat": 10, "per_hour": 5})
        assert epa.participation_amount(cfg, 20.0, False) == 0

    def test_flat_plus_hours_rounds_half_up(self):
        cfg = _cfg(participation={"flat": 10, "per_hour": 2})
        # 12.25h × 2 = 24.5 → 25, plus the flat 10.
        assert epa.participation_amount(cfg, 12.25, True) == 35

    def test_hours_rounded_to_display_precision_first(self):
        cfg = _cfg(participation={"per_hour": 250})
        # 1.004h displays as 1.00h and pays 250 — the preview's arithmetic —
        # not the 251 the unrounded hours would.
        assert epa.participation_amount(cfg, 1.004, True) == 250

    def test_floor_blocks_everything_below_it(self):
        cfg = _cfg(participation={"flat": 10, "per_hour": 2, "min_hours": 3})
        assert epa.participation_amount(cfg, 2.99, True) == 0
        assert epa.participation_amount(cfg, 3.0, True) == 16

    def test_cap(self):
        cfg = _cfg(participation={"flat": 10, "per_hour": 10, "max": 50})
        assert epa.participation_amount(cfg, 100, True) == 50

    def test_took_part_with_no_hours_gets_flat(self):
        cfg = _cfg(participation={"flat": 7, "per_hour": 3})
        assert epa.participation_amount(cfg, 0, True) == 7

    def test_competition_pays_flat_only(self):
        cfg = _cfg(participation={"flat": 7, "per_hour": 3, "min_hours": 5})
        assert epa.participation_amount(cfg, 0, True, ehe_supported=False) == 7


def _members():
    return [
        {"player_id": 1, "player_name": "Alpha", "team_id": 10},
        {"player_id": 2, "player_name": "Bravo", "team_id": 10},
        {"player_id": 3, "player_name": "Charlie", "team_id": 20},
        {"player_id": 4, "player_name": "Delta", "team_id": 20},
        {"player_id": 5, "player_name": "Echo", "team_id": 30},
    ]


class TestPlanAwards:
    def test_team_placement_each_member_and_participation(self):
        cfg = _cfg(enabled=True, placement=[100, 50],
                   participation={"per_hour": 2})
        plan = epa.plan_awards(
            cfg, members=_members(), placements={10: 1, 20: 2},
            hours={1: 5.0, 2: 1.2, 3: 0.0, 5: 3.0}, took_part={1, 2, 3, 5},
            clan_member_ids={1, 2, 3, 4, 5})
        by = {r["player_id"]: r for r in plan["rows"]}
        assert by[1]["placement"] == 100 and by[1]["participation"] == 10
        assert by[2]["placement"] == 100 and by[2]["participation"] == 2
        assert by[3]["placement"] == 50 and by[3]["participation"] == 0
        # Delta took no part: no placement (active_only) and no participation.
        assert 4 not in by
        assert {"player_id": 4, "player_name": "Delta", "team_id": 20,
                "reason": "inactive"} in plan["skipped"]
        # Echo's team didn't place, but they still earn participation.
        assert by[5]["placement"] == 0 and by[5]["participation"] == 6
        # Best-paid first.
        assert [r["player_id"] for r in plan["rows"]] == [1, 2, 3, 5]

    def test_active_only_off_pays_the_whole_team(self):
        cfg = _cfg(enabled=True, placement=[100, 50], placement_active_only=False)
        plan = epa.plan_awards(cfg, members=_members(), placements={10: 1, 20: 2},
                               hours={}, took_part=set(), clan_member_ids={1, 2, 3, 4, 5})
        assert sorted(r["player_id"] for r in plan["rows"]) == [1, 2, 3, 4]
        assert plan["skipped"] == []

    def test_non_members_are_never_paid(self):
        cfg = _cfg(enabled=True, placement=[100], participation={"flat": 5})
        plan = epa.plan_awards(cfg, members=_members(), placements={10: 1},
                               hours={}, took_part={1, 2, 5}, clan_member_ids={1})
        assert [r["player_id"] for r in plan["rows"]] == [1]
        reasons = {s["player_id"]: s["reason"] for s in plan["skipped"]}
        assert reasons == {2: "not_member", 5: "not_member"}

    def test_competition_places_players_not_teams(self):
        cfg = _cfg(enabled=True, placement=[30, 20, 10], participation={"flat": 1})
        members = [{"player_id": p, "player_name": f"P{p}", "team_id": 99}
                   for p in (1, 2, 3)]
        plan = epa.plan_awards(cfg, members=members, placements={2: 1, 3: 1},
                               hours={}, took_part={1, 2, 3}, clan_member_ids={1, 2, 3},
                               competition=True, ehe_supported=False)
        by = {r["player_id"]: r for r in plan["rows"]}
        # A tie for 1st pays both the 1st-place amount.
        assert by[2]["placement"] == by[3]["placement"] == 30
        assert by[1]["placement"] == 0 and by[1]["participation"] == 1
        assert by[1]["hours"] is None

    def test_duplicate_roster_rows_counted_once(self):
        cfg = _cfg(enabled=True, placement=[10])
        members = _members()[:1] * 2
        plan = epa.plan_awards(cfg, members=members, placements={10: 1}, hours={},
                               took_part={1}, clan_member_ids={1})
        assert len(plan["rows"]) == 1


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
class TestLedger:
    def _plan(self):
        return {"rows": [
            {"player_id": 1, "team_id": 10, "place": 1, "placement": 100,
             "participation": 10, "hours": 5.0, "total": 110},
            {"player_id": 3, "team_id": 20, "place": None, "placement": 0,
             "participation": 4, "hours": 2.0, "total": 4},
        ]}

    def test_desired_ledger_splits_kinds(self):
        desired = epa.desired_ledger(self._plan())
        assert desired == {
            (1, "placement"): {"amount": 100, "team_id": 10, "place": 1, "hours": None},
            (1, "participation"): {"amount": 10, "team_id": 10, "place": None, "hours": 5.0},
            (3, "participation"): {"amount": 4, "team_id": 20, "place": None, "hours": 2.0},
        }

    def test_first_award_inserts_everything(self):
        ops = epa.diff_ledger(epa.desired_ledger(self._plan()), {})
        assert ops["insert"] == [(1, "participation"), (1, "placement"), (3, "participation")]
        assert not (ops["update"] or ops["delete"] or ops["unchanged"] or ops["reset"])

    def test_resync_after_a_revoke(self):
        existing = {
            (1, "placement"): {"amount": 100, "points_row": True},
            (1, "participation"): {"amount": 12, "points_row": True},   # hours fell
            (2, "placement"): {"amount": 50, "points_row": True},       # team dropped out
        }
        ops = epa.diff_ledger(epa.desired_ledger(self._plan()), existing)
        assert ops["unchanged"] == [(1, "placement")]
        assert ops["update"] == [(1, "participation")]
        assert ops["delete"] == [(2, "placement")]
        assert ops["insert"] == [(3, "participation")]

    def test_points_reset_is_not_resurrected(self):
        existing = {(1, "placement"): {"amount": 100, "points_row": False}}
        ops = epa.diff_ledger({(1, "placement"): {"amount": 150}}, existing)
        assert ops["reset"] == [(1, "placement")]
        assert not (ops["insert"] or ops["update"])


class TestText:
    def test_ordinals(self):
        assert [epa.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)] == [
            "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"]

    def test_reasons(self):
        assert epa.award_reason("Summer Bingo", "placement", place=1, team_name="Red") == \
            "Event: Summer Bingo — 1st place (Red)"
        assert epa.award_reason("Summer Bingo", "participation", hours=12.5) == \
            "Event: Summer Bingo — participation (12.5h EHE)"
        assert epa.award_reason("Summer Bingo", "participation") == \
            "Event: Summer Bingo — participation"

    def test_reason_fits_the_column(self):
        assert len(epa.award_reason("x" * 200, "placement", place=2, team_name="y" * 50)) == 125

    def test_clan_points_line(self):
        line = epa.clan_points_line([{
            "group_name": "Pegasus",
            "placements": [
                {"place": 1, "label": "Red", "amount": 100, "players": 4, "team": True},
                {"place": 2, "label": "Blue", "amount": 50, "players": 0, "team": True},
                {"place": 4, "label": "Solo", "amount": 5, "players": 1, "team": False},
            ],
            "participation_points": 1234, "participation_players": 31, "total_points": 1639,
        }])
        # Blue was placed but nobody was paid (all inactive) — not announced.
        assert line == ("\U0001FA99 Clan points: \U0001F947 **Red** `+100` each"
                        " · 4th **Solo** `+5`"
                        " · participation `+1,234` across 31 members")

    def test_clan_points_line_multi_clan_and_empty(self):
        results = [
            {"group_name": "A", "placements": [{"place": 1, "label": "A team", "amount": 10,
                                                "players": 2, "team": True}],
             "participation_points": 0, "total_points": 20},
            {"group_name": "B", "placements": [], "participation_points": 3,
             "participation_players": 1, "total_points": 3},
            {"group_name": "C", "placements": [], "total_points": 0},
        ]
        assert epa.clan_points_line(results) == (
            "\U0001FA99 **A** — Clan points: \U0001F947 **A team** `+10` each\n"
            "\U0001FA99 **B** — Clan points: participation `+3` across 1 member")
        assert epa.clan_points_line([]) is None
        assert epa.clan_points_line([{"group_name": "C", "total_points": 0}]) is None
