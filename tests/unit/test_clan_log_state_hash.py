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


def test_the_period_is_part_of_the_hash():
    # Switching clan_log_period (or the month rolling over) must edit the
    # standing message even when the same slots are obtained.
    assert img.board_state_hash(_payload(period="2026")) != img.board_state_hash(_payload())


def test_period_label_and_board_link_follow_the_period():
    assert cld.period_label("all") == "all time"
    assert cld.period_label("2026") == "2026"
    assert cld.period_label("2026-10") == "October 2026"
    assert cld.board_url(381) == "https://www.droptracker.io/groups/381/log"
    assert cld.board_url(381, "2026-10").endswith("/groups/381/log?period=2026-10")
    text = cld.build_message_text(_payload(period="2026-10"))
    assert "(October 2026)" in text and "?period=2026-10" in text


class _FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _fake_clan_log(monkeypatch, stored, ledger):
    """services.clan_log over an in-memory store, as of October 2026. The
    real standing_period is covered in test_clan_log.py."""
    saved = {}
    fake = types.SimpleNamespace(
        PERIOD_ALL="all",
        standing_period=lambda s: {"year": "2026", "month": "2026-10"}.get(s, "all"),
        load_board=lambda _s, gid, period: stored.get(period),
        ledger_periods=lambda _s, gid: ledger,
        build_payload=lambda _s, gid, period: _payload(period=period),
        save_board=lambda _s, gid, period, payload: saved.__setitem__(period, payload),
    )
    monkeypatch.setitem(sys.modules, "services.clan_log", fake)
    return saved


def test_standing_board_reads_the_configured_window(monkeypatch):
    stored = {"all": _payload(), "2026": _payload(period="2026")}
    saved = _fake_clan_log(monkeypatch, stored, ["all", "2026", "2026-10"])
    assert cld._standing_board(_FakeSession(), 381, "year") == ("2026", stored["2026"])
    assert cld._standing_board(_FakeSession(), 381, None) == ("all", stored["all"])
    assert saved == {}


def test_standing_board_builds_a_new_month_the_sweep_has_not_reached(monkeypatch):
    saved = _fake_clan_log(monkeypatch, {"all": _payload()}, ["all", "2026", "2026-09"])
    session = _FakeSession()
    period, payload = cld._standing_board(session, 381, "month")
    assert period == "2026-10" and payload["period"] == "2026-10"
    assert set(saved) == {"2026-10"} and session.commits == 1


def test_standing_board_never_mints_a_board_for_an_unswept_group(monkeypatch):
    saved = _fake_clan_log(monkeypatch, {}, [])
    assert cld._standing_board(_FakeSession(), 381, "month") == ("2026-10", None)
    assert saved == {}
