"""PvP kill tracking: intake, counting rules, standings, boards and messages.

A player kill reaches intake as a "drop" payload, one embed per item, tagged
``source_type=pvp`` with the victim's name as the source. These tests pin the
pieces that keep it out of the NPC pipeline and count it fairly:

* ``collapse_pvp_bundle`` folds those embeds into one ``pvp_kill``;
* the global and per-group counting rules (minigames, own accounts, clanmate
  kills, the same-victim daily cap);
* the ``pvp:`` Redis standings;
* the board kinds in ``lootboard/schedule.py``;
* the notification wiring (channel, default embed, Components V2 layout).
"""

import fnmatch
import importlib
import importlib.util
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from data.submissions import pvp
from data.submissions.common import GROUP_CHANNEL_NOTIFICATION_KEYS
from data.submissions.dispatch import (
    SEASONAL_TYPES,
    SUPPORTED_TYPES,
    normalize_submission_type,
)
from web_api import config_registry as reg

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_REPO, relative))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# conftest stubs the ``services`` package, so the modules under test are
# loaded from their files.
standings = _load("_pvp_standings_under_test", "services/pvp_standings.py")
schedule = _load("_lootboard_schedule_under_test", "lootboard/schedule.py")


def _item(name, item_id, qty, value, guid, *, victim="Zezima", source_type="pvp", **extra):
    data = {
        "type": "drop",
        "source_type": source_type,
        "source": victim,
        "item": name,
        "id": str(item_id),
        "quantity": str(qty),
        "value": str(value),
        "guid": guid,
        "player_name": "Killer",
        "acc_hash": "123",
        "auth_key": "k",
        "p_v": "6.1.9",
    }
    data.update(extra)
    return data


# ── Bundle collapse ──────────────────────────────────────────────────────────

class TestCollapse:
    def test_a_payload_with_no_pvp_embed_is_returned_as_is(self):
        items = [_item("Abyssal whip", 4151, 1, 1, "g1", source_type="npc")]
        assert pvp.collapse_pvp_bundle(items) is items

    def test_item_embeds_become_one_kill(self):
        items = [
            _item("Abyssal whip", 4151, 1, 1_400_000, "g1"),
            _item("Shark", 385, 4, 900, "g2"),
        ]
        out = pvp.collapse_pvp_bundle(items)
        assert len(out) == 1
        kill = out[0]
        assert kill["type"] == "pvp_kill"
        assert kill["victim_name"] == "Zezima"
        assert kill["guid"] == "g1"
        assert kill["_bundle_guids"] == ["g1", "g2"]
        assert [i["item_name"] for i in kill["pvp_items"]] == ["Abyssal whip", "Shark"]
        assert kill["pvp_items"][1]["quantity"] == "4"
        # Envelope fields survive; per-item ones do not leak onto the kill.
        assert kill["acc_hash"] == "123" and kill["p_v"] == "6.1.9"
        assert "item" not in kill and "id" not in kill

    def test_other_embeds_in_the_payload_are_left_alone(self):
        npc = _item("Bones", 526, 1, 0, "g9", source_type="npc", victim="Goblin")
        out = pvp.collapse_pvp_bundle([npc, _item("Rune scimitar", 1333, 1, 15_000, "g1")])
        assert out[0] is npc
        assert out[1]["type"] == "pvp_kill"

    def test_two_victims_in_one_payload_are_two_kills(self):
        out = pvp.collapse_pvp_bundle([
            _item("Shark", 385, 1, 900, "a", victim="One"),
            _item("Shark", 385, 1, 900, "b", victim="Two"),
            _item("Bones", 526, 1, 0, "c", victim="one"),
        ])
        assert [k["victim_name"] for k in out] == ["One", "Two"]
        assert len(out[0]["pvp_items"]) == 2

    def test_an_item_declared_as_pvp_outright_is_folded_too(self):
        out = pvp.collapse_pvp_bundle([_item("Shark", 385, 1, 900, "a", type="pvp")])
        assert out[0]["type"] == "pvp_kill"

    def test_a_collapsed_kill_is_not_collapsed_again(self):
        once = pvp.collapse_pvp_bundle([_item("Shark", 385, 1, 900, "a")])
        assert pvp.collapse_pvp_bundle(once) is once

    def test_the_kill_routes_to_its_own_processor_main_world_only(self):
        assert "pvp_kill" in SUPPORTED_TYPES
        assert "pvp_kill" not in SEASONAL_TYPES
        assert normalize_submission_type("pvp") == "pvp_kill"


# ── Counting rules ───────────────────────────────────────────────────────────

def _player(pid, user_id):
    return SimpleNamespace(player_id=pid, user_id=user_id)


class TestGlobalExclusion:
    def test_an_lms_kill_counts_nowhere(self):
        assert pvp.global_exclusion(13658, _player(1, 10), None) == "pvp_minigame:lms"

    def test_minigames_are_named(self):
        assert pvp.global_exclusion(13362, _player(1, 10), None) == "pvp_minigame:pvp_arena"
        assert pvp.global_exclusion(10588, _player(1, 10), None) == "pvp_minigame:pk_tutor"

    def test_killing_your_own_alt_counts_nowhere(self):
        assert pvp.global_exclusion(12345, _player(1, 10), _player(2, 10)) == "own_account"

    def test_user_id_zero_is_a_real_owner(self):
        assert pvp.global_exclusion(None, _player(1, 0), _player(2, 0)) == "own_account"

    def test_untracked_or_unowned_victims_count(self):
        assert pvp.global_exclusion(12345, _player(1, 10), None) is None
        assert pvp.global_exclusion(12345, _player(1, 10), _player(2, 11)) is None
        assert pvp.global_exclusion(12345, _player(1, None), _player(2, None)) is None


class TestSameVictimLimit:
    def test_the_default_is_three(self):
        assert pvp.same_victim_limit({}, 7) == 3

    def test_zero_and_junk_fall_back_to_the_default(self):
        assert pvp.same_victim_limit({(7, pvp.SAME_VICTIM_LIMIT_KEY): "0"}, 7) == 3
        assert pvp.same_victim_limit({(7, pvp.SAME_VICTIM_LIMIT_KEY): "x"}, 7) == 3

    def test_a_saved_limit_is_used(self):
        assert pvp.same_victim_limit({(7, pvp.SAME_VICTIM_LIMIT_KEY): "5"}, 7) == 5

    def test_the_unlimited_switch_wins(self):
        config = {(7, pvp.SAME_VICTIM_LIMIT_KEY): "5", (7, pvp.SAME_VICTIM_UNLIMITED_KEY): "1"}
        assert pvp.same_victim_limit(config, 7) is None


class TestGroupVerdict:
    @pytest.fixture
    def kill(self):
        return SimpleNamespace(id=9, player_id=1, victim_name="Zezima",
                               date_added=datetime(2026, 10, 19, 12))

    def test_a_clanmate_kill_does_not_count_by_default(self, kill, monkeypatch):
        monkeypatch.setattr(pvp, "_counted_today", lambda *a, **k: 0)
        assert pvp.group_verdict(None, kill, 7, {}, {7}) == "member_kill"

    def test_a_group_can_count_clanmate_kills(self, kill, monkeypatch):
        monkeypatch.setattr(pvp, "_counted_today", lambda *a, **k: 0)
        config = {(7, pvp.COUNT_MEMBER_KILLS_KEY): "1"}
        assert pvp.group_verdict(None, kill, 7, config, {7}) is None

    def test_the_global_group_never_applies_the_clanmate_rule(self, kill, monkeypatch):
        # Every tracked player is in group 2, so the rule would void every kill.
        monkeypatch.setattr(pvp, "_counted_today", lambda *a, **k: 0)
        assert pvp.group_verdict(None, kill, 2, {}, {2, 7}) is None

    def test_kills_past_the_daily_cap_do_not_count(self, kill, monkeypatch):
        monkeypatch.setattr(pvp, "_counted_today", lambda *a, **k: 3)
        assert pvp.group_verdict(None, kill, 7, {}, set()) == "same_victim_limit"
        monkeypatch.setattr(pvp, "_counted_today", lambda *a, **k: 2)
        assert pvp.group_verdict(None, kill, 7, {}, set()) is None

    def test_no_cap_when_unlimited(self, kill, monkeypatch):
        monkeypatch.setattr(pvp, "_counted_today", lambda *a, **k: 50)
        config = {(7, pvp.SAME_VICTIM_UNLIMITED_KEY): "true"}
        assert pvp.group_verdict(None, kill, 7, config, set()) is None


class TestPvpDeath:
    def test_only_player_kills_on_the_main_game_count(self):
        assert pvp.is_pvp_death("player", 12345, "main")
        assert not pvp.is_pvp_death("npc", 12345, "main")
        assert not pvp.is_pvp_death("player", 12345, "seasonal")
        assert not pvp.is_pvp_death("player", 13658, "main")  # LMS


# ── Redis standings ──────────────────────────────────────────────────────────

class FakeRedis:
    """Just the sorted-set and list commands pvp_standings uses."""

    def __init__(self):
        self.z = {}
        self.l = {}
        self.ttl = {}

    def pipeline(self, transaction=False):
        return FakePipe(self)

    def zincrby(self, key, amount, member):
        zset = self.z.setdefault(key, {})
        zset[str(member)] = zset.get(str(member), 0) + amount

    def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update({str(m): s for m, s in mapping.items()})

    def expire(self, key, ttl):
        self.ttl[key] = ttl

    def lpush(self, key, value):
        self.l.setdefault(key, []).insert(0, value)

    def rpush(self, key, *values):
        self.l.setdefault(key, []).extend(values)

    def ltrim(self, key, start, end):
        self.l[key] = self.l.get(key, [])[start:end + 1]

    def lrange(self, key, start, end):
        return self.l.get(key, [])[start:end + 1]

    def zscore(self, key, member):
        return self.z.get(key, {}).get(str(member))

    def zmscore(self, key, members):
        return [self.zscore(key, m) for m in members]

    def zrevrank(self, key, member):
        ranked = sorted(self.z.get(key, {}).items(), key=lambda kv: -kv[1])
        for i, (m, _s) in enumerate(ranked):
            if m == str(member):
                return i
        return None

    def zrevrangebyscore(self, key, high, low, start=0, num=None, withscores=False):
        rows = sorted(((m, s) for m, s in self.z.get(key, {}).items() if s > 0),
                      key=lambda kv: -kv[1])
        return rows[start:start + num] if num is not None else rows[start:]

    def zcount(self, key, low, high):
        return sum(1 for s in self.z.get(key, {}).values() if s > 0)

    def zscan_iter(self, key, count=None):
        return iter(self.z.get(key, {}).items())

    def scan_iter(self, match=None, count=None):
        keys = list(self.z) + list(self.l)
        return iter([k for k in keys if match is None or fnmatch.fnmatch(k, match)])

    def delete(self, *keys):
        removed = 0
        for key in keys:
            removed += int(self.z.pop(key, None) is not None or self.l.pop(key, None) is not None)
        return removed


class FakePipe:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return queue

    def execute(self):
        out = [getattr(self.r, n)(*a, **k) for n, a, k in self.ops]
        self.ops = []
        return out


def _kill(pid=1, value=1_000_000, when=datetime(2026, 10, 19, 12), kill_id=5):
    return SimpleNamespace(
        id=kill_id, player_id=pid, victim_name="Zezima", victim_player_id=None,
        total_value=value, item_count=3, wilderness_level=30, world=318,
        date_added=when,
        items=[SimpleNamespace(item_id=4151, item_name="Abyssal whip", quantity=1, value=value)],
    )


class TestStandings:
    def test_keys_scope_the_global_group_to_the_unscoped_board(self):
        assert standings.board_key("loot", "202610") == "pvp:loot:202610"
        assert standings.board_key("loot", "202610", 2) == "pvp:loot:202610"
        assert standings.board_key("kills", "all", 7) == "pvp:kills:all:group:7"
        with pytest.raises(ValueError):
            standings.board_key("xp", "all")

    def test_a_kill_counts_on_every_period_and_scope_it_counted_for(self):
        r = FakeRedis()
        assert standings.record_kill(_kill(), [2, 7], player_name="Killer", client=r)
        for token in ("202610", "2026W43", "20261019", "all"):
            assert r.z[f"pvp:loot:{token}"]["1"] == 1_000_000
            assert r.z[f"pvp:kills:{token}:group:7"]["1"] == 1
            assert r.z[f"pvp:gloot:{token}"]["7"] == 1_000_000
        assert "pvp:loot:all" not in r.ttl
        assert r.ttl["pvp:loot:20261019"] == standings._DAILY_TTL
        assert len(r.l["pvp:recent:group:7"]) == 1

    def test_a_kill_that_counted_nowhere_writes_nothing(self):
        r = FakeRedis()
        assert standings.record_kill(_kill(), [], client=r)
        assert r.z == {}

    def test_removing_a_kill_takes_it_back_off(self):
        r = FakeRedis()
        standings.record_kill(_kill(), [7], client=r)
        standings.remove_kill(_kill(), [7], client=r)
        rows, total = standings.top("loot", "202610", 7, client=r)
        assert rows == [] and total == 0

    def test_reads_rank_and_sum(self):
        r = FakeRedis()
        standings.record_kill(_kill(pid=1, value=500), [7], client=r)
        standings.record_kill(_kill(pid=2, value=900, kill_id=6), [7], client=r)
        standings.record_kill(_kill(pid=2, value=100, kill_id=7), [7], client=r)
        standings.record_death(1, [7], datetime(2026, 10, 19), client=r)
        rows, total = standings.top("loot", "202610", 7, client=r)
        assert rows == [(2, 1000), (1, 500)] and total == 2
        line = standings.player_line(1, "202610", 7, client=r)
        assert line == {"loot": 500, "kills": 1, "deaths": 1, "rank": 2}
        assert standings.board_sum("kills", "202610", 7, client=r) == 3
        assert standings.group_total(7, "202610", client=r) == 1500
        assert standings.scores_for("kills", "202610", [1, 2, 3], 7, client=r) == {1: 1, 2: 2, 3: 0}

    def test_a_death_counts_globally_and_for_each_group(self):
        r = FakeRedis()
        standings.record_death(4, [7, 9], datetime(2026, 10, 19), client=r)
        assert r.z["pvp:deaths:202610"]["4"] == 1
        assert r.z["pvp:deaths:202610:group:9"]["4"] == 1

    def test_a_redis_fault_never_raises(self):
        broken = MagicMock()
        broken.pipeline.side_effect = ConnectionError("down")
        assert standings.record_kill(_kill(), [7], client=broken) is False
        assert standings.record_death(1, [7], client=broken) is False

    def test_periods_resolve_like_the_drop_boards(self):
        assert standings.resolve_token("all") == "all"
        assert standings.resolve_token("202610") == "202610"


# ── Board kinds ──────────────────────────────────────────────────────────────

class TestBoardKinds:
    def test_the_drop_board_keeps_its_names(self):
        assert schedule.board_path(7).endswith("/clans/7/lb/lootboard.png")
        assert schedule.dirty_key(7) == "lootboard:dirty:7"
        assert schedule.posted_key(7) == "lootboard:posted:7"
        assert schedule.cooldown_key(7) == "lootboard:instant_cd:7"

    def test_the_pvp_board_has_its_own(self):
        assert schedule.board_path(7, schedule.PVP).endswith("/clans/7/lb/pvp_lootboard.png")
        keys = {schedule.dirty_key(7, "pvp"), schedule.posted_key(7, "pvp"),
                schedule.cooldown_key(7, "pvp")}
        assert all(k.startswith("lootboard:pvp:") for k in keys)
        # A scan for drop-board dirty flags must never pick up PvP ones.
        assert not schedule.dirty_key(7, "pvp").startswith("lootboard:dirty:")

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            schedule.board_path(7, "xp")


# ── Settings + notifications ─────────────────────────────────────────────────

class TestSettings:
    def test_the_pvp_fields_exist_in_their_own_category(self):
        assert {"key": "pvp", "label": "PvP"} in reg.CONFIG_CATEGORIES
        for key in (pvp.CHANNEL_KEY, pvp.MIN_VALUE_KEY, pvp.COUNT_MEMBER_KILLS_KEY,
                    pvp.SAME_VICTIM_LIMIT_KEY, pvp.SAME_VICTIM_UNLIMITED_KEY,
                    pvp.NOTIFY_DEATHS_KEY, pvp.DEATH_MIN_VALUE_KEY,
                    "pvp_lootboard_channel_id", "pvp_lootboard_message_id",
                    "pvp_loot_board_type", "pvp_repost_lootboard"):
            field = reg.get_config_field(key)
            assert field is not None, key
            assert field["category"] == "pvp"

    def test_defaults_match_the_processor(self):
        assert reg.get_config_field(pvp.MIN_VALUE_KEY)["default"] == pvp.DEFAULT_MIN_VALUE
        assert reg.get_config_field(pvp.SAME_VICTIM_LIMIT_KEY)["default"] == pvp.DEFAULT_SAME_VICTIM_LIMIT

    def test_pvp_never_falls_back_to_the_drops_channel(self):
        assert GROUP_CHANNEL_NOTIFICATION_KEYS["pvp_kill"] == ("channel_id_to_post_pvp",)
        assert GROUP_CHANNEL_NOTIFICATION_KEYS["pvp_death"] == ("channel_id_to_post_pvp",)

    def test_visible_copy_has_no_em_dashes(self):
        for field in reg.GROUP_CONFIG_FIELDS:
            if field["category"] == "pvp":
                assert "—" not in field["label"] + field["help"], field["key"]


@pytest.fixture(scope="module")
def ns():
    for name in ("services.contribution_notifications", "services.event_notifications"):
        sys.modules.setdefault(name, MagicMock())
    return _load("_pvp_notifications_under_test", "services/notification_service.py")


class TestMessages:

    def test_placeholders_drop_what_an_old_plugin_did_not_send(self, ns):
        values = ns.NotificationService._pvp_placeholder_map({
            "victim_name": "Zezima", "total_value": 12_400_000, "items_text": "Abyssal whip (1.4M)",
            "top_item_id": 4151, "kills_this_month": 3, "pvp_rank": 2,
        })
        assert values["{pvp_value}"] == "12.40M"
        assert values["{victim_name}"] == "Zezima"  # untracked: no link
        assert values["{item_icon}"].endswith("/itemdb/4151.png")
        assert values["{pvp_rank}"] == "#2"
        assert values["{victim_combat_level}"] == ""
        assert values["{wilderness_level}"] == ""

    def test_a_tracked_victim_is_linked(self, ns):
        values = ns.NotificationService._pvp_placeholder_map(
            {"victim_name": "Zezima", "victim_player_id": 5})
        assert values["{victim_name}"] == "[Zezima](https://www.droptracker.io/players/5)"

    def test_every_template_token_is_documented_for_its_type(self, ns):
        import re

        cl = importlib.import_module("services.component_layout")
        defaults = importlib.import_module("web_api.routes.notification_defaults")
        for kind in ("pvp_kill", "pvp_death"):
            assert kind in cl.NOTIFICATION_TYPES
            documented = {t["token"] for t in cl.tokens_for(kind)}
            embed = defaults.BUILTIN_EMBEDS[kind]
            text = " ".join([embed["title"], embed["description"], embed["thumbnail"] or ""]
                            + [f["value"] for f in embed["fields"]])
            layout = str(cl.default_layout(kind))
            for token in re.findall(r"\{([a-z_]+)\}", text + layout):
                assert token in documented, (kind, token)
            assert set(documented) - set(cl.TOKEN_DOCS) == set()


# ── Loot keys (ticket 498) ───────────────────────────────────────────────────

def _key_item(name, item_id, qty, value, guid, **extra):
    """One item of an opened loot key, as the plugin forwards it today: an
    ordinary drop whose source is RuneLite's "Loot Chest"."""
    return _item(name, item_id, qty, value, guid, victim="Loot Chest",
                 source_type="other", **extra)


class TestLootKeyCollapse:
    def test_an_opened_key_becomes_one_loot_key_kill(self):
        out = pvp.collapse_pvp_bundle([
            _key_item("Abyssal whip", 4151, 1, 1_400_000, "g1"),
            _key_item("Shark", 385, 10, 900, "g2"),
        ])
        assert len(out) == 1
        kill = out[0]
        assert kill["type"] == pvp.PVP_KILL_TYPE
        assert kill["loot_key"] is True
        assert kill["victim_name"] == pvp.UNKNOWN_VICTIM
        assert kill["_bundle_guids"] == ["g1", "g2"]
        assert kill["guid"] == "g1"

    def test_the_chest_name_matches_in_any_case(self):
        assert pvp.is_loot_chest_embed({"type": "drop", "source": "loot chest"})
        assert pvp.is_loot_chest_embed({"type": "other", "npc_name": "Loot Chest"})

    def test_other_chests_and_other_types_are_left_alone(self):
        assert not pvp.is_loot_chest_embed({"type": "drop", "source": "Lunar Chest"})
        assert not pvp.is_loot_chest_embed({"type": "pb", "source": "Loot Chest"})
        out = pvp.collapse_pvp_bundle([_item("Moon helm", 1, 1, 1, "g", victim="Lunar Chest",
                                             source_type="npc")])
        assert out[0]["type"] == "drop"

    def test_a_kill_and_a_key_in_one_payload_stay_apart(self):
        out = pvp.collapse_pvp_bundle([
            _item("Bones", 526, 1, 33, "k1"),
            _key_item("Abyssal whip", 4151, 1, 1_400_000, "g1"),
        ])
        assert len(out) == 2
        assert {k.get("loot_key", False) for k in out} == {True, False}
        assert next(k for k in out if not k.get("loot_key"))["victim_name"] == "Zezima"

    def test_a_key_routes_to_the_pvp_processor(self):
        kill = pvp.collapse_pvp_bundle([_key_item("Abyssal whip", 4151, 1, 1, "g1")])[0]
        assert normalize_submission_type(kill["type"]) == "pvp_kill"


class TestKeyedKillShape:
    @staticmethod
    def _items(*names):
        return [SimpleNamespace(item_name=n) for n in names]

    def test_bones_only_and_bones_with_the_key_qualify(self):
        assert pvp.is_keyed_kill_shape(self._items("Bones"))
        assert pvp.is_keyed_kill_shape(self._items("Loot key", "Bones"))

    def test_a_kill_with_real_loot_on_the_floor_does_not(self):
        assert not pvp.is_keyed_kill_shape(self._items("Bones", "Abyssal whip"))
        assert not pvp.is_keyed_kill_shape([])


class TestLootKeyMatching:
    """process_loot_key_burst: match only when it is certain."""

    @pytest.fixture
    def harness(self, monkeypatch):
        calls = {"merged": [], "recorded": [], "candidates": []}
        session = MagicMock()
        player = SimpleNamespace(player_id=5773933)

        async def auth(*_a, **_k):
            return player, True, True

        async def resolve(_s, raw, _name):
            return [{"item_id": 1, "item_name": "x", "quantity": 1, "value": 10, "total": 10}]

        async def merge(_s, kill, kill_data, *_a):
            calls["merged"].append((kill, kill_data["guid"]))

        async def record(_s, kill_data, *_a):
            calls["recorded"].append(kill_data["guid"])

        monkeypatch.setattr(pvp, "select_session_and_flag", lambda _e: (session, False))
        monkeypatch.setattr(pvp, "ensure_player_and_auth", auth)
        monkeypatch.setattr(pvp, "_resolve_items", resolve)
        monkeypatch.setattr(pvp, "_loot_key_seen", lambda _s, guid: guid == "seen")
        monkeypatch.setattr(pvp, "received_at", lambda _d: datetime(2026, 10, 23, 22, 30))
        monkeypatch.setattr(pvp, "loot_key_candidates", lambda *_a: calls["candidates"])
        monkeypatch.setattr(pvp, "_merge_loot_key", merge)
        monkeypatch.setattr(pvp, "_record_loot_key", record)
        calls["session"] = session
        return calls

    @staticmethod
    def _key(guid):
        return {"player_name": "sits pure 1", "acc_hash": "1", "guid": guid,
                "pvp_items": [{"item_id": 1}]}

    @pytest.mark.asyncio
    async def test_one_key_and_one_candidate_is_matched(self, harness):
        harness["candidates"].append("kill-1247")
        assert await pvp.process_loot_key_burst([self._key("g1")]) == 1
        assert harness["merged"] == [("kill-1247", "g1")]
        assert harness["recorded"] == []
        harness["session"].commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_two_candidates_is_ambiguous(self, harness):
        harness["candidates"].extend(["kill-1", "kill-2"])
        await pvp.process_loot_key_burst([self._key("g1")])
        assert harness["merged"] == [] and harness["recorded"] == ["g1"]

    @pytest.mark.asyncio
    async def test_no_candidate_is_recorded_on_its_own(self, harness):
        await pvp.process_loot_key_burst([self._key("g1")])
        assert harness["merged"] == [] and harness["recorded"] == ["g1"]

    @pytest.mark.asyncio
    async def test_several_keys_opened_together_are_never_matched(self, harness):
        harness["candidates"].append("kill-1")
        await pvp.process_loot_key_burst([self._key("g1"), self._key("g2")])
        assert harness["merged"] == [] and harness["recorded"] == ["g1", "g2"]

    @pytest.mark.asyncio
    async def test_unmatchable_mode_records_even_with_one_candidate(self, harness):
        harness["candidates"].append("kill-1")
        await pvp.process_loot_key_burst([self._key("g1")], matchable=False)
        assert harness["merged"] == [] and harness["recorded"] == ["g1"]

    @pytest.mark.asyncio
    async def test_a_replayed_key_is_skipped(self, harness):
        harness["candidates"].append("kill-1")
        assert await pvp.process_loot_key_burst([self._key("seen")]) == 0
        assert harness["merged"] == [] and harness["recorded"] == []


class FakeZRedis:
    """Just the sorted-set calls the loot key sweep makes."""

    def __init__(self):
        self.z = {}

    def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)

    def zrangebyscore(self, key, lo, hi, start=0, num=None):
        rows = sorted((s, m) for m, s in self.z.get(key, {}).items() if s <= hi)
        return [m.encode() for _, m in rows][start:(start + num) if num else None]

    def zrange(self, key, start, end):
        rows = sorted((s, m) for m, s in self.z.get(key, {}).items())
        return [m.encode() for _, m in rows]

    def zrem(self, key, member):
        member = member.decode() if isinstance(member, bytes) else member
        return 1 if self.z.get(key, {}).pop(member, None) is not None else 0


class TestLootKeySweep:
    @pytest.fixture
    def env(self, monkeypatch):
        import json
        r = FakeZRedis()
        bursts = []

        async def burst(keys, matchable=True):
            bursts.append(sorted(k["guid"] for k in keys))
            return len(keys)

        monkeypatch.setattr(pvp, "_redis", lambda: r)
        monkeypatch.setattr(pvp, "process_loot_key_burst", burst)

        def park(guid, player, arrived, due):
            entry = {"kill_data": {"player_name": player, "acc_hash": "1", "guid": guid},
                     "arrived": arrived, "attempts": 0}
            r.zadd(pvp.LOOT_KEY_DEFERRED_KEY, {json.dumps(entry): due})

        return r, bursts, park

    @pytest.mark.asyncio
    async def test_keys_of_one_opening_replay_together(self, env):
        r, bursts, park = env
        park("a", "sits", 100.0, 145.0)
        park("b", "sits", 101.0, 1e12)  # not due yet, same opening
        park("c", "sits", 500.0, 1e12)  # a later opening
        park("d", "other", 100.0, 1e12)  # someone else, not due
        assert await pvp.process_due_loot_keys() == 2
        assert bursts == [["a", "b"]]
        assert len(r.z[pvp.LOOT_KEY_DEFERRED_KEY]) == 2

    @pytest.mark.asyncio
    async def test_nothing_due_does_nothing(self, env):
        r, bursts, park = env
        park("a", "sits", 100.0, 1e12)
        assert await pvp.process_due_loot_keys() == 0
        assert bursts == []

    @pytest.mark.asyncio
    async def test_an_opening_is_parked_not_processed_on_arrival(self, env, monkeypatch):
        r, bursts, _park = env
        kill = pvp.collapse_pvp_bundle([_key_item("Abyssal whip", 4151, 1, 1, "g1")])[0]
        resp = await pvp.pvp_kill_processor(kill)
        assert resp.success
        assert bursts == []
        assert len(r.z[pvp.LOOT_KEY_DEFERRED_KEY]) == 1


class TestLootKeyStandings:
    def test_an_unmatched_key_adds_loot_but_no_kill(self):
        r = FakeRedis()
        key = _kill(value=2_791_529)
        key.kind = "loot_key"
        assert standings.record_kill(key, [7], client=r)
        assert r.z["pvp:loot:202610:group:7"]["1"] == 2_791_529
        assert "pvp:kills:202610:group:7" not in r.z

    def test_a_matched_key_adds_loot_on_the_kills_own_date(self):
        r = FakeRedis()
        kill = _kill(value=33)
        standings.record_kill(kill, [2, 7], client=r)
        assert standings.add_loot(kill, 2_791_496, [2, 7], client=r)
        assert r.z["pvp:loot:20261019"]["1"] == 2_791_529
        assert r.z["pvp:kills:20261019:group:7"]["1"] == 1
        assert r.z["pvp:gloot:202610"]["7"] == 2_791_529

    def test_removing_an_unmatched_key_takes_no_kill_off(self):
        r = FakeRedis()
        standings.record_kill(_kill(), [7], client=r)
        key = _kill(value=500, kill_id=6)
        key.kind = "loot_key"
        standings.record_kill(key, [7], client=r)
        standings.remove_kill(key, [7], client=r)
        assert r.z["pvp:kills:202610:group:7"]["1"] == 1
        assert r.z["pvp:loot:202610:group:7"]["1"] == 1_000_000


class TestLootKeyMessages:
    def test_an_unmatched_key_names_the_key_not_a_victim(self, ns):
        values = ns.NotificationService._pvp_placeholder_map(
            {"victim_name": "", "loot_key": True, "total_value": 2_791_529})
        assert values["{victim_name}"] == "a loot key"

    def test_a_matched_key_keeps_its_victim(self, ns):
        values = ns.NotificationService._pvp_placeholder_map(
            {"victim_name": "master chefy", "loot_key": True})
        assert values["{victim_name}"] == "master chefy"
