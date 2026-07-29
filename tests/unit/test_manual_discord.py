"""Unit tests for data/submissions/manual_discord.py — the pure helpers
behind the Discord /submit commands.

The Extension itself (commands/submissions.py) can't be imported here: the
conftest stubs ``interactions`` as a MagicMock, so Extension subclasses fail
at class-creation. All submission-shaping logic therefore lives in the pure
module under test.
"""

import pytest

from data.submissions.manual_discord import (
    CA_TIERS,
    SUBMISSION_TYPES,
    build_manual_payload,
    format_ms,
    parse_kill_time_ms,
    payload_to_form,
    summarize_submission,
)


# ── parse_kill_time_ms ────────────────────────────────────────────────────────
# Mirrors the web repo's parseKillTimeMs (components/submit-form.tsx) so both
# surfaces accept the same formats.

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1:23.40", 83_400),
        ("0:45", 45_000),
        ("1:02:03.6", 3_723_600),
        ("83.4", 83_400),
        ("45", 45_000),
        (" 1:23.40 ", 83_400),  # whitespace tolerated
        ("2:00", 120_000),
    ],
)
def test_parse_kill_time_valid(raw, expected):
    assert parse_kill_time_ms(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", None, "abc", "1:2:3:4", "1:234", "-1:00", "0", "0.0",
        "1.2345",  # >3 fractional digits
        "1:23,40",  # wrong separator
    ],
)
def test_parse_kill_time_invalid(raw):
    assert parse_kill_time_ms(raw) is None


def test_format_ms_round_trips_display():
    assert format_ms(83_400) == "1:23.4"
    assert format_ms(45_000) == "0:45"
    assert format_ms(3_723_600) == "1:02:03.6"


# ── build_manual_payload ──────────────────────────────────────────────────────

def test_drop_payload_full():
    p = build_manual_payload(
        "drop", "Test Player",
        item_name="Dragon claws", npc_name="Chambers of Xeric",
        quantity=2, value=180_000_000,
    )
    assert p == {
        "submission_type": "drop",
        "player_name": "Test Player",
        "world_type": "main",
        "item_name": "Dragon claws",
        "npc_name": "Chambers of Xeric",
        "quantity": 2,
        "value": 180_000_000,
    }


def test_drop_payload_omits_value_for_ge_lookup():
    p = build_manual_payload("drop", "P", item_name="Twisted bow", npc_name="CoX")
    assert "value" not in p  # intake defaults to 0 -> GE price recovery
    assert p["quantity"] == 1


def test_drop_requires_item_and_npc():
    with pytest.raises(ValueError):
        build_manual_payload("drop", "P", item_name="Twisted bow")
    with pytest.raises(ValueError):
        build_manual_payload("drop", "P", npc_name="CoX")


def test_clog_payload_source_and_kc_optional():
    p = build_manual_payload("clog", "P", item_name="Smoke battlestaff",
                             npc_name="Thermonuclear smoke devil", kc=1234)
    assert p["submission_type"] == "collection_log"
    assert p["source"] == "Thermonuclear smoke devil"
    assert p["kc"] == 1234
    bare = build_manual_payload("clog", "P", item_name="Smoke battlestaff")
    assert "source" not in bare and "kc" not in bare


def test_pb_payload():
    p = build_manual_payload("pb", "P", npc_name="Zulrah", time_ms=83_400, team_size=1)
    assert p["submission_type"] == "personal_best"
    assert p["boss_name"] == "Zulrah"
    assert p["time_ms"] == 83_400
    assert p["team_size"] == 1
    with pytest.raises(ValueError):
        build_manual_payload("pb", "P", npc_name="Zulrah")  # no time
    with pytest.raises(ValueError):
        build_manual_payload("pb", "P", time_ms=1000)  # no boss


def test_ca_payload_normalizes_tier():
    p = build_manual_payload("ca", "P", task=" Perfect Zulrah ", tier="Grandmaster")
    assert p["submission_type"] == "combat_achievement"
    assert p["task"] == "Perfect Zulrah"
    assert p["tier"] == "grandmaster"
    assert p["tier"] in CA_TIERS


def test_pet_payload_maps_pet_name_and_killcount():
    p = build_manual_payload("pet", "P", item_name="Vorki", npc_name="Vorkath", kc=500)
    assert p["submission_type"] == "pet"
    assert p["pet_name"] == "Vorki"
    assert p["source"] == "Vorkath"
    assert p["killcount"] == 500
    assert "kc" not in p  # pet processor reads killcount, not kc


def test_unknown_type_rejected():
    with pytest.raises(ValueError):
        build_manual_payload("quest", "P")


def test_all_types_map_to_intake_contract():
    # Guard the Discord-key -> intake submission_type mapping against drift
    # from web_api/routes/submissions.py's _TYPE_MAP.
    assert SUBMISSION_TYPES == {
        "drop": "drop",
        "clog": "collection_log",
        "pb": "personal_best",
        "ca": "combat_achievement",
        "pet": "pet",
    }


# ── payload_to_form ───────────────────────────────────────────────────────────

def test_payload_to_form_stringifies_for_multipart():
    form = payload_to_form({
        "submission_type": "drop", "quantity": 3, "value": 0,
        "is_pb": True, "duplicate": False, "skipped": None,
    })
    # Intake's multipart parser converts digit strings back to ints and
    # true/false back to bools; None values must be omitted entirely.
    assert form == {
        "submission_type": "drop", "quantity": "3", "value": "0",
        "is_pb": "true", "duplicate": "false",
    }


# ── summarize_submission ──────────────────────────────────────────────────────

def test_summaries_read_naturally():
    drop = build_manual_payload("drop", "P", item_name="Dragon claws",
                                npc_name="CoX", quantity=3)
    assert summarize_submission("drop", drop) == "3x **Dragon claws** from **CoX**"
    single = build_manual_payload("drop", "P", item_name="Dragon claws", npc_name="CoX")
    assert summarize_submission("drop", single) == "**Dragon claws** from **CoX**"
    pb = build_manual_payload("pb", "P", npc_name="Zulrah", time_ms=83_400)
    assert summarize_submission("pb", pb) == "**1:23.4** at **Zulrah**"
    team_pb = build_manual_payload("pb", "P", npc_name="CoX", time_ms=60_000, team_size=3)
    assert "team of 3" in summarize_submission("pb", team_pb)
    ca = build_manual_payload("ca", "P", task="Perfect Zulrah", tier="elite")
    assert summarize_submission("ca", ca) == "**Perfect Zulrah** (Elite combat achievement)"
    pet = build_manual_payload("pet", "P", item_name="Vorki", npc_name="Vorkath")
    assert summarize_submission("pet", pet) == "**Vorki** (pet) from **Vorkath**"
