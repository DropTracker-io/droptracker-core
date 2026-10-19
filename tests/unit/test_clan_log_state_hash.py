"""Clan Log standing message: change detection and post order (ticket #476)."""
import copy
import importlib.util
import os
import sys
import types

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


img = _load("clan_log_image_under_test", "services/clan_log_image.py")
cld = _load("clan_log_discord_under_test", "services/clan_log_discord.py")


def _payload(**over):
    p = {
        "group_id": 381, "group_name": "Too Hard", "period": "all",
        "catalog_version": "v2", "generated_at": "2026-10-19T17:00:00",
        "summary": {"obtained": 2, "total": 677},
        "sections": [{"slug": "cox", "items": [
            {"item_id": 20997, "obtained": True, "player_id": 5771724, "count": 1},
            {"item_id": 21003, "obtained": False},
            {"item_id": 21043, "obtained": True, "player_id": 7, "count": 2},
        ]}],
        "recent": [{"item_id": 20997, "at": "2026-10-18T10:00:00"}],
    }
    p.update(over)
    return p


def test_a_resweep_with_nothing_new_is_not_a_change():
    a = _payload()
    b = _payload(generated_at="2026-10-19T17:20:00")
    assert img.board_state_hash(a) == img.board_state_hash(b)


def test_a_new_slot_count_holder_or_catalog_is_a_change():
    base = img.board_state_hash(_payload())
    more = _payload()
    more["sections"][0]["items"][1].update(obtained=True, player_id=9, count=1)
    assert img.board_state_hash(more) != base

    recount = copy.deepcopy(_payload())
    recount["sections"][0]["items"][2]["count"] = 3
    assert img.board_state_hash(recount) != base

    assert img.board_state_hash(_payload(catalog_version="v3")) != base
    assert img.board_state_hash(_payload(group_name="Too Hard II")) != base


def test_has_message_reads_unset_sentinels_as_never_posted():
    cfg = {(1, "clan_log_message_id"): "155", (2, "clan_log_message_id"): "0",
           (3, "clan_log_message_id"): " "}
    assert cld._has_message(cfg, 1) is True
    assert cld._has_message(cfg, 2) is False
    assert cld._has_message(cfg, 3) is False
    assert cld._has_message(cfg, 4) is False
