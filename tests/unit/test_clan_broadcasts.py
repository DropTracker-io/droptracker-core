"""Unit tests for utils/clan_broadcasts.py — the clan-relay broadcast parser.

Fixture strings mirror real in-game broadcast lines (including client markup
the relay forwards verbatim). Pure functions, no DB.
"""
import pytest

from utils.clan_broadcasts import (
    TRACKED_KINDS,
    ParsedBroadcast,
    clean_broadcast_text,
    parse_broadcast,
)


def test_clean_strips_markup_nbsp_and_whitespace():
    raw = "<img=41>Iron Botanist   received a drop: <col=ff0000>Dragon pickaxe</col> (1,171,463 coins)."
    assert (
        clean_broadcast_text(raw)
        == "Iron Botanist received a drop: Dragon pickaxe (1,171,463 coins)."
    )
    assert clean_broadcast_text(None) == ""
    assert clean_broadcast_text("   ") == ""


# --- item_drop -------------------------------------------------------------

def test_item_drop_with_value():
    parsed = parse_broadcast("RuneScape Player received a drop: Abyssal whip (1,456,814 coins).")
    assert parsed == ParsedBroadcast(
        kind="item_drop",
        player="RuneScape Player",
        item_name="Abyssal whip",
        quantity=1,
        value_gp=1456814,
    )
    assert parsed.tracked


def test_item_drop_with_quantity():
    parsed = parse_broadcast("Bulk Buyer received a drop: 1,000 x Pure essence (4,000 coins).")
    assert parsed.kind == "item_drop"
    assert parsed.quantity == 1000
    assert parsed.item_name == "Pure essence"
    assert parsed.value_gp == 4000


def test_item_drop_untradeable_has_no_value():
    parsed = parse_broadcast("Zuk Enjoyer received a drop: Infernal cape.")
    assert parsed.kind == "item_drop"
    assert parsed.item_name == "Infernal cape"
    assert parsed.value_gp is None


def test_item_drop_parenthesized_item_name_is_not_a_value():
    parsed = parse_broadcast("Slayer Main received a drop: Black mask (10) (1,200,000 coins).")
    assert parsed.item_name == "Black mask (10)"
    assert parsed.value_gp == 1200000

    no_value = parse_broadcast("Slayer Main received a drop: Black mask (10).")
    assert no_value.item_name == "Black mask (10)"
    assert no_value.value_gp is None


def test_item_drop_with_ironman_icon_prefix():
    parsed = parse_broadcast("<img=41>Iron Botanist received a drop: Magic seed (170,000 coins).")
    assert parsed.player == "Iron Botanist"
    assert parsed.item_name == "Magic seed"


# --- raid_drop / clue_item -------------------------------------------------

def test_raid_drop_has_no_value_in_text():
    parsed = parse_broadcast("Lucky One received special loot from a raid: Twisted bow.")
    assert parsed == ParsedBroadcast(
        kind="raid_drop", player="Lucky One", item_name="Twisted bow"
    )


def test_clue_item():
    parsed = parse_broadcast("Clue Solver received a clue item: Ranger boots (36,460,467 coins).")
    assert parsed.kind == "clue_item"
    assert parsed.item_name == "Ranger boots"
    assert parsed.value_gp == 36460467


def test_clue_item_without_value():
    parsed = parse_broadcast("Clue Solver received a clue item: 3rd age full helmet.")
    assert parsed.kind == "clue_item"
    assert parsed.item_name == "3rd age full helmet"
    assert parsed.value_gp is None


# --- pet -------------------------------------------------------------------

def test_pet_followed_variant():
    parsed = parse_broadcast(
        "Pet Hunter has a funny feeling like he's being followed: Butch at 194 kills."
    )
    assert parsed.kind == "pet"
    assert parsed.player == "Pet Hunter"
    assert parsed.item_name == "Butch"
    assert parsed.extra == {"milestone_count": 194, "milestone_unit": "kills"}


def test_pet_backpack_variant_with_female_pronoun():
    parsed = parse_broadcast(
        "Rift Runner feels something weird sneaking into her backpack: Abyssal protector at 543 rift searches."
    )
    assert parsed.kind == "pet"
    assert parsed.item_name == "Abyssal protector"
    assert parsed.extra == {"milestone_count": 543, "milestone_unit": "rift searches"}


def test_pet_duplicate_variant_would_have_been_followed():
    parsed = parse_broadcast(
        "Grinder has a funny feeling like she would have been followed: Ikkle hydra at 5,000 kills."
    )
    assert parsed.kind == "pet"
    assert parsed.item_name == "Ikkle hydra"
    assert parsed.extra["milestone_count"] == 5000


def test_pet_xp_milestone_unit():
    parsed = parse_broadcast(
        "Fisher has a funny feeling like he's being followed: Heron at 12,000,000 XP."
    )
    assert parsed.extra == {"milestone_count": 12000000, "milestone_unit": "XP"}


# --- collection_log --------------------------------------------------------

def test_collection_log():
    parsed = parse_broadcast(
        "KANlEL OUTIS received a new collection log item: Elite void robe (170/1477)"
    )
    assert parsed.kind == "collection_log"
    assert parsed.player == "KANlEL OUTIS"
    assert parsed.item_name == "Elite void robe"
    assert parsed.extra == {"log_slots": 170, "log_total": 1477}


def test_collection_log_with_trailing_period():
    parsed = parse_broadcast(
        "Completionist received a new collection log item: Smoke battlestaff (1200/1477)."
    )
    assert parsed.kind == "collection_log"
    assert parsed.extra["log_slots"] == 1200


# --- classify-only kinds stay out of the unknown bucket --------------------

@pytest.mark.parametrize(
    "line,kind,player",
    [
        ("Quester has completed a quest: Dragon Slayer II.", "quest", "Quester"),
        (
            "Diary Andy has completed the Elite Lumbridge & Draynor diary.",
            "diary",
            "Diary Andy",
        ),
        ("Th3TRiPPyOn3 has reached Defence level 70.", "level_up", "Th3TRiPPyOn3"),
        (
            "Maxed Soon has reached the highest possible Attack level of 99.",
            "level_up",
            "Maxed Soon",
        ),
        ("Noble Five has reached 78,000,000 XP in Fishing.", "xp_milestone", "Noble Five"),
        (
            "Victor Locke has been invited into the clan by IRuneNakey.",
            "invite",
            "Victor Locke",
        ),
        ("Quitter has left the clan.", "left_clan", "Quitter"),
        ("Main Dangler has been defeated by Koishi Fumo in The Wilderness.", "pk", "Main Dangler"),
        (
            "Generous One has deposited 5,000,000 coins into the coffer.",
            "coffer_donation",
            "Generous One",
        ),
        (
            "Treasurer has withdrawn 2,000,000 coins from the coffer.",
            "coffer_withdrawal",
            "Treasurer",
        ),
    ],
)
def test_classify_only_kinds(line, kind, player):
    parsed = parse_broadcast(line)
    assert parsed is not None, line
    assert parsed.kind == kind
    assert parsed.player == player
    assert not parsed.tracked


def test_pk_win_extracts_value_and_loser():
    parsed = parse_broadcast(
        "KANlEL OUTIS has defeated Emperor KB and received (972,728 coins) worth of loot!"
    )
    assert parsed.kind == "pk"
    assert parsed.value_gp == 972728
    assert parsed.extra == {"defeated": "Emperor KB", "won": True}


def test_expelled_extracts_the_expelled_player():
    parsed = parse_broadcast("Strict Mod has expelled Rule Breaker from the clan.")
    assert parsed.kind == "expelled"
    assert parsed.player == "Rule Breaker"


def test_raid_pb_carries_bracket_activity_and_time():
    parsed = parse_broadcast(
        "Raid Leader has achieved a new Chambers of Xeric (Team Size: 5) personal best: 21:55.80"
    )
    assert parsed is not None
    assert parsed.kind == "personal_best"
    assert parsed.tracked
    assert parsed.player == "Raid Leader"
    assert parsed.extra == {
        "activity": "Chambers of Xeric",
        "team_size": "5",
        "time_text": "21:55.80",
    }


def test_solo_pb_has_no_bracket_in_line():
    parsed = parse_broadcast("Speed Runner has achieved a new Vorkath personal best: 1:04.")
    assert parsed is not None
    assert parsed.kind == "personal_best"
    assert parsed.tracked
    assert parsed.extra == {"activity": "Vorkath", "team_size": None, "time_text": "1:04"}


# --- unknown ---------------------------------------------------------------

def test_unknown_shapes_return_none():
    assert parse_broadcast("To talk in your clan's channel, start each line of chat with // or /c.") is None
    assert parse_broadcast("gz!!") is None
    assert parse_broadcast("") is None
    assert parse_broadcast(None) is None


def test_player_chatter_that_mentions_drops_does_not_false_positive():
    # A member typing about a drop is CLAN_CHAT, not CLAN_MESSAGE, so it never
    # reaches the parser in production — but a colon-free paraphrase must not
    # match anyway.
    assert parse_broadcast("imagine if I received a drop lol") is None


def test_tracked_kinds_is_exactly_v2():
    assert TRACKED_KINDS == {
        "item_drop",
        "raid_drop",
        "clue_item",
        "pet",
        "collection_log",
        "personal_best",
    }
