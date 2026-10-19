"""Pairing logic of scripts/dedupe_clan_broadcast_copies.py (pure, no DB)."""
import importlib.util
import os
from datetime import datetime, timedelta

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "dedupe_clan_broadcast_copies.py")
_spec = importlib.util.spec_from_file_location("dedupe_cc_under_test", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

T0 = datetime(2026, 10, 19, 12, 41, 34)


def _row(drop_id, seconds, player=5772293, name="runite ore", qty=27):
    return {"drop_id": drop_id, "player_id": player, "name": name, "quantity": qty,
            "date_added": T0 + timedelta(seconds=seconds)}


def test_ticket_491_pair_is_found():
    chat = [_row(243919855, 3)]
    plugin = [_row(243916599, 0)]
    pairs = mod.pair_copies(chat, plugin, 600)
    assert [(c["drop_id"], p["drop_id"]) for c, p in pairs] == [(243919855, 243916599)]


def test_one_plugin_row_never_absorbs_two_copies():
    chat = [_row(10, 3), _row(11, 5)]
    plugin = [_row(1, 0)]
    assert len(mod.pair_copies(chat, plugin, 600)) == 1


def test_each_copy_takes_the_nearest_unclaimed_twin():
    chat = [_row(10, 0), _row(11, 300)]
    plugin = [_row(1, 2), _row(2, 301)]
    pairs = {c["drop_id"]: p["drop_id"] for c, p in mod.pair_copies(chat, plugin, 600)}
    assert pairs == {10: 1, 11: 2}


def test_no_pair_outside_window_or_on_other_player_name_or_quantity():
    chat = [_row(10, 0)]
    assert mod.pair_copies(chat, [_row(1, 601)], 600) == []
    assert mod.pair_copies(chat, [_row(1, 1, player=7)], 600) == []
    assert mod.pair_copies(chat, [_row(1, 1, name="coal")], 600) == []
    assert mod.pair_copies(chat, [_row(1, 1, qty=26)], 600) == []


def test_plugin_twin_arriving_after_the_copy_still_pairs():
    assert len(mod.pair_copies([_row(10, 0)], [_row(1, 400)], 600)) == 1


def test_hour_bucket_matches_rollup_date_format():
    assert mod.hour_bucket(datetime(2026, 10, 19, 7, 5, 59)) == "2026-10-19-07"


def test_applied_snapshots_keep_their_plugin_rows_claimed(tmp_path):
    import json

    (tmp_path / "dedupe_clan_broadcast_copies_1.json").write_text(json.dumps(
        {"applied": True, "pairs": [{"chat": {"drop_id": 206487567},
                                     "plugin_twin": {"drop_id": 206483828}}]}))
    (tmp_path / "dedupe_clan_broadcast_copies_2.json").write_text(json.dumps(
        {"applied": False, "pairs": [{"chat": {"drop_id": 1}, "plugin_twin": {"drop_id": 2}}]}))
    (tmp_path / "dedupe_clan_broadcast_copies_3.json").write_text("not json")
    assert mod.previously_absorbed(str(tmp_path)) == {206483828}
