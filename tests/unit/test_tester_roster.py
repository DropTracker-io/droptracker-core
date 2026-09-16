"""The Bug Tester roster: built in production, sealed, applied on dev.

What these pin:

* the snapshot carries every account of every tester and nothing that is not
  a tester's (an inactive badge makes nobody one);
* the seal authenticates — a wrong key, a stale token or a malformed body is
  refused, never half-applied;
* applying a snapshot converges: repeated, it changes nothing; with a tester
  removed, their award is revoked;
* ids are a hint, not an identity. Dev hands out the same auto-increment ids
  as production for different people, so a production id that is taken on
  dev must never overwrite someone else's row;
* an older snapshot can never undo a newer one.
"""
from __future__ import annotations

import json
import sys
import time

import pytest

from tests.unit import _tester_db as tdb

tr = tdb.load("_tester_roster_under_test", "services", "tester_roster.py")

KEY = "c2VjcmV0LWtleS1mb3ItdGVzdHMtMDEyMzQ1Njc4OTA="  # 32 bytes, urlsafe base64


@pytest.fixture()
def prod(monkeypatch):
    factory = tdb.make_sessionmaker()
    tdb.install_models(monkeypatch, factory)
    return factory


@pytest.fixture()
def dev(monkeypatch):
    factory = tdb.make_sessionmaker()
    tdb.install_models(monkeypatch, factory)
    return factory


def _prod_with_one_tester(factory):
    with factory() as s:
        s.add(tdb.User(user_id=5, discord_id="111", username="tester", auth_token="x" * 16))
        s.add(tdb.User(user_id=6, discord_id="222", username="bystander", auth_token="y" * 16))
        s.add_all([
            tdb.Player(player_id=50, player_name="Main", account_hash="-900", wom_id=500, user_id=5),
            tdb.Player(player_id=51, player_name="Alt", account_hash=" 901 ", wom_id=501, user_id=5),
            tdb.Player(player_id=60, player_name="Other", account_hash="902", wom_id=600, user_id=6),
            tdb.Player(player_id=70, player_name="Unlinked", account_hash="903", wom_id=700),
        ])
        s.flush()
        badge = tdb.add_badge(s)
        tdb.award(s, badge, 50)
        tdb.award(s, badge, 60, status="revoked")
        tdb.award(s, badge, 70)  # no user: not a tester
        s.commit()
        return tr.load_roster(s)


class TestConstants:
    def test_badge_key_matches_the_role_sync(self):
        roles = tdb.load("_discord_roles_const", "services", "discord_roles.py")
        assert tr.BUG_TESTER_BADGE_KEY == roles.BUG_TESTER_BADGE_KEY

    def test_digest_cache_key_matches_edge_config(self):
        ec = tdb.load("_edge_config_const", "services", "edge_config.py")
        assert tr.DIGEST_CACHE_KEY == ec.DIGEST_CACHE_KEY

    def test_tester_ids_key_matches_the_dm_guard(self):
        from utils import dev_guild_guard

        assert tr.DEV_DISCORD_IDS_KEY == dev_guild_guard.TESTER_IDS_KEY


class TestLoadRoster:
    def test_every_account_of_a_tester_and_nobody_else(self, prod):
        roster = _prod_with_one_tester(prod)
        assert [u["user_id"] for u in roster["users"]] == [5]
        assert [p["player_id"] for p in roster["players"]] == [50, 51]
        assert [a["player_id"] for a in roster["awards"]] == [50]

    def test_hashes_are_stripped_as_the_plugin_sends_them(self, prod):
        roster = _prod_with_one_tester(prod)
        assert tr.account_hashes(roster) == ["-900", "901"]

    def test_no_auth_token_leaves_production(self, prod):
        roster = _prod_with_one_tester(prod)
        assert "auth_token" not in json.dumps(roster)

    def test_an_inactive_badge_makes_nobody_a_tester(self, prod):
        _prod_with_one_tester(prod)
        with prod() as s:
            s.query(tdb.Badge).update({"active": False})
            s.commit()
            roster = tr.load_roster(s)
        assert roster["users"] == [] and roster["awards"] == []
        assert roster["badge"]["active"] is False

    def test_no_badge_at_all(self, prod):
        with prod() as s:
            roster = tr.load_roster(s)
        assert roster["badge"] is None and roster["users"] == []


class TestFingerprint:
    def test_ignores_when_it_was_taken(self, prod):
        roster = _prod_with_one_tester(prod)
        later = dict(roster, ts=roster["ts"] + 60)
        assert tr.fingerprint(roster) == tr.fingerprint(later)

    def test_changes_with_the_content(self, prod):
        roster = _prod_with_one_tester(prod)
        changed = json.loads(json.dumps(roster))
        changed["players"][0]["player_name"] = "Renamed"
        assert tr.fingerprint(roster) != tr.fingerprint(changed)


class TestSeal:
    def test_round_trip(self, prod):
        roster = _prod_with_one_tester(prod)
        opened = tr.unseal(tr.seal(roster, KEY), KEY)
        assert opened["users"] == roster["users"]

    def test_the_hashes_are_not_readable_in_transit(self, prod):
        roster = _prod_with_one_tester(prod)
        assert "-900" not in tr.seal(roster, KEY)

    def test_wrong_key_is_refused(self, prod):
        roster = _prod_with_one_tester(prod)
        other = "b3RoZXIta2V5LWZvci10ZXN0cy0wMTIzNDU2Nzg5MDE="
        with pytest.raises(tr.InvalidSnapshot):
            tr.unseal(tr.seal(roster, KEY), other)

    def test_a_stale_token_is_refused(self):
        from cryptography.fernet import Fernet

        body = json.dumps(tr.empty_roster()).encode()
        old = Fernet(KEY.encode()).encrypt_at_time(body, int(time.time()) - 3600).decode()
        with pytest.raises(tr.InvalidSnapshot):
            tr.unseal(old, KEY)

    @pytest.mark.parametrize("payload", [
        [1, 2],
        {"v": 99, "ts": 1, "users": [], "players": [], "awards": []},
        {"v": 1, "ts": 1, "users": {}, "players": [], "awards": []},
        {"v": 1, "ts": "soon", "users": [], "players": [], "awards": []},
        {"v": 1, "ts": 1, "badge": "x", "users": [], "players": [], "awards": []},
    ])
    def test_malformed_payloads_are_refused(self, payload):
        from cryptography.fernet import Fernet

        token = Fernet(KEY.encode()).encrypt(json.dumps(payload).encode()).decode()
        with pytest.raises(tr.InvalidSnapshot):
            tr.unseal(token, KEY)

    def test_garbage_is_refused(self):
        with pytest.raises(tr.InvalidSnapshot):
            tr.unseal("not a token", KEY)


def _snapshot(users=(), players=(), awards=(), active=True, ts=None):
    return {
        "v": 1, "ts": ts if ts is not None else time.time(),
        "badge": {"key": "bug_tester_helper", "name": "Bug Tester", "description": "d",
                  "icon_url": None, "icon_emoji": None, "tone": "green",
                  "semantic": "permanent", "scope": "global", "active": active, "criteria": None},
        "users": list(users), "players": list(players), "awards": list(awards),
    }


TESTER = {"user_id": 5, "discord_id": "111", "username": "tester"}
MAIN = {"player_id": 50, "player_name": "Main", "account_hash": "-900", "wom_id": 500,
        "user_id": 5, "total_level": 2000, "log_slots": 900, "account_type": "ironman"}
ALT = {"player_id": 51, "player_name": "Alt", "account_hash": "901", "wom_id": 501,
       "user_id": 5, "total_level": 100, "log_slots": 1, "account_type": None}
AWARD = {"player_id": 50, "slot_key": "p:50", "group_key": 0,
         "awarded_at": "2026-09-14T12:00:00", "context": None}


def _apply(factory, snapshot):
    with factory() as s:
        result = tr.apply_roster(s, snapshot)
        s.commit()
    return result


def _active_awards(factory):
    with factory() as s:
        return sorted((a.player_id, a.slot_key) for a in
                      s.query(tdb.PlayerBadge).filter(tdb.PlayerBadge.status == "active"))


class TestApplyOnAFreshDevDatabase:
    def test_creates_the_tester_their_accounts_and_the_award(self, dev):
        result = _apply(dev, _snapshot([TESTER], [MAIN, ALT], [AWARD]))
        assert (result.users_added, result.players_added, result.awards_added) == (1, 2, 1)
        assert result.tester_user_ids == [5]
        assert result.tester_discord_ids == ["111"]
        assert _active_awards(dev) == [(50, "p:50")]
        with dev() as s:
            user = s.get(tdb.User, 5)
            assert user.discord_id == "111"
            assert len(user.auth_token) == 16, "a fresh token, never production's"
            assert s.get(tdb.Player, 50).account_type == "ironman"

    def test_applying_twice_changes_nothing(self, dev):
        snap = _snapshot([TESTER], [MAIN, ALT], [AWARD])
        _apply(dev, snap)
        again = _apply(dev, snap)
        assert (again.users_added, again.players_added, again.awards_added,
                again.awards_revoked) == (0, 0, 0, 0)
        assert _active_awards(dev) == [(50, "p:50")]

    def test_a_removed_tester_loses_the_award(self, dev):
        _apply(dev, _snapshot([TESTER], [MAIN, ALT], [AWARD]))
        result = _apply(dev, _snapshot())
        assert result.awards_revoked == 1
        assert result.tester_user_ids == []
        assert _active_awards(dev) == []

    def test_an_inactive_badge_revokes_everything(self, dev):
        _apply(dev, _snapshot([TESTER], [MAIN, ALT], [AWARD]))
        result = _apply(dev, _snapshot([TESTER], [MAIN, ALT], [AWARD], active=False))
        assert result.awards_revoked == 1
        assert result.tester_user_ids == []

    def test_a_snapshot_with_no_badge_clears_dev_awards(self, dev):
        _apply(dev, _snapshot([TESTER], [MAIN, ALT], [AWARD]))
        empty = tr.empty_roster()
        result = _apply(dev, empty)
        assert result.awards_revoked == 1
        assert _active_awards(dev) == []


class TestIdsAreHintsNotIdentities:
    def test_a_taken_user_id_does_not_overwrite_someone_else(self, dev):
        with dev() as s:
            s.add(tdb.User(user_id=5, discord_id="999", username="dev-local", auth_token="z" * 16))
            s.commit()
        result = _apply(dev, _snapshot([TESTER], [MAIN], [AWARD]))
        with dev() as s:
            assert s.get(tdb.User, 5).discord_id == "999", "the dev-local user is untouched"
            tester = s.query(tdb.User).filter(tdb.User.discord_id == "111").one()
            assert tester.user_id != 5
            assert s.get(tdb.Player, 50).user_id == tester.user_id
        assert result.tester_discord_ids == ["111"]

    def test_the_same_discord_account_under_another_id_is_reused(self, dev):
        with dev() as s:
            s.add(tdb.User(user_id=77, discord_id="111", username="signed-in-on-dev", auth_token="z" * 16))
            s.commit()
        result = _apply(dev, _snapshot([TESTER], [MAIN], [AWARD]))
        assert result.users_added == 0
        with dev() as s:
            assert s.query(tdb.User).count() == 1
            assert s.get(tdb.Player, 50).user_id == 77
        assert result.tester_user_ids == [77]

    def test_a_taken_player_id_does_not_overwrite_another_account(self, dev):
        with dev() as s:
            s.add(tdb.User(user_id=8, discord_id="888", auth_token="z" * 16))
            s.add(tdb.Player(player_id=50, player_name="Someone", account_hash="12345",
                             wom_id=4242, user_id=8))
            s.commit()
        result = _apply(dev, _snapshot([TESTER], [MAIN], [AWARD]))
        with dev() as s:
            other = s.get(tdb.Player, 50)
            assert (other.account_hash, other.user_id) == ("12345", 8)
            main = s.query(tdb.Player).filter(tdb.Player.account_hash == "-900").one()
            assert main.player_id != 50
        assert _active_awards(dev) == [(main.player_id, f"p:{main.player_id}")]
        assert result.tester_discord_ids == ["111"]

    def test_the_same_account_under_another_id_is_reused(self, dev):
        with dev() as s:
            s.add(tdb.Player(player_id=1234, player_name="main", account_hash="-900", wom_id=None))
            s.commit()
        result = _apply(dev, _snapshot([TESTER], [MAIN], [AWARD]))
        assert result.players_added == 0
        with dev() as s:
            row = s.get(tdb.Player, 1234)
            assert (row.player_name, row.wom_id, row.user_id) == ("Main", 500, 5)
        assert _active_awards(dev) == [(1234, "p:1234")]

    def test_a_split_identity_is_reported_not_guessed(self, dev):
        with dev() as s:
            s.add(tdb.Player(player_id=300, player_name="a", account_hash="-900"))
            s.add(tdb.Player(player_id=301, player_name="b", wom_id=500))
            s.commit()
        result = _apply(dev, _snapshot([TESTER], [MAIN], [AWARD]))
        assert any("dev player 300" in note and "dev player 301" in note
                   for note in result.skipped), result.skipped
        assert _active_awards(dev) == []

    def test_a_dump_stub_with_no_identity_is_reused_by_name(self, dev):
        with dev() as s:
            s.add(tdb.Player(player_id=50, player_name="main"))
            s.commit()
        _apply(dev, _snapshot([TESTER], [MAIN], [AWARD]))
        with dev() as s:
            assert s.query(tdb.Player).count() == 1
            assert s.get(tdb.Player, 50).account_hash == "-900"


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self
        return record

    def execute(self):
        for name, args, kwargs in self.ops:
            getattr(self.redis, name)(*args, **kwargs)
        self.ops = []


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.sets = {}
        self.lists = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        value = self.values.get(key)
        return value.encode() if isinstance(value, str) else value

    def delete(self, key):
        self.values.pop(key, None)
        self.sets.pop(key, None)
        self.lists.pop(key, None)

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(str(m) for m in members)

    def smembers(self, key):
        return {m.encode() for m in self.sets.get(key, set())}

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start:end + 1]

    def pipeline(self, transaction=True):
        return FakePipeline(self)


@pytest.fixture()
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(tr, "_redis", lambda: fake)
    return fake


@pytest.fixture()
def snapshot_env(dev, monkeypatch, redis):
    """apply_snapshot's collaborators: the badge-group module and no BADGE_GROUPS."""
    tdb.load("_badge_groups_for_roster", "db", "badge_groups.py",
             register_as="db.badge_groups", monkeypatch=monkeypatch)
    monkeypatch.delenv("BADGE_GROUPS", raising=False)
    return dev


class TestApplySnapshot:
    def test_applies_and_publishes_the_dm_allowlist(self, snapshot_env, redis):
        status, body = tr.apply_snapshot(_snapshot([TESTER], [MAIN], [AWARD]))
        assert status == 200 and body["status"] == "applied"
        assert body["testers"] == 1
        assert redis.sets[tr.DEV_DISCORD_IDS_KEY] == {"111"}
        assert tr.DEV_APPLY_LOCK_KEY not in redis.values, "the lock is released"

    def test_an_older_snapshot_cannot_undo_a_newer_one(self, snapshot_env, redis):
        now = time.time()
        tr.apply_snapshot(_snapshot([TESTER], [MAIN], [AWARD], ts=now))
        status, body = tr.apply_snapshot(_snapshot(ts=now - 30))
        assert (status, body["status"]) == (200, "stale")
        assert _active_awards(snapshot_env) == [(50, "p:50")]

    def test_one_writer_at_a_time(self, snapshot_env, redis):
        redis.values[tr.DEV_APPLY_LOCK_KEY] = "someone-else"
        status, body = tr.apply_snapshot(_snapshot([TESTER], [MAIN], [AWARD]))
        assert (status, body["status"]) == (409, "busy")
        assert redis.values[tr.DEV_APPLY_LOCK_KEY] == "someone-else", "not ours to release"

    def test_badge_groups_follow_immediately(self, snapshot_env, redis, monkeypatch):
        with snapshot_env() as s:
            s.add(tdb.Group(group_id=10000001, group_name="Bug Testers", guild_id="1"))
            s.commit()
        monkeypatch.setenv("BADGE_GROUPS", '{"10000001":"bug_tester_helper"}')
        status, body = tr.apply_snapshot(_snapshot([TESTER], [MAIN, ALT], [AWARD]))
        assert body["groups"] == {"10000001": {"added": 2, "removed": 0}}
        with snapshot_env() as s:
            rows = s.execute(tdb.user_group_association.select()).fetchall()
            assert sorted(r.player_id for r in rows) == [50, 51]


class TestNotifyChanged:
    def test_wakes_the_pusher_and_drops_the_digest_cache(self, redis):
        redis.values[tr.DIGEST_CACHE_KEY] = "[]"
        assert tr.notify_changed() is True
        assert redis.lists[tr.REQUEST_KEY] == ["1"]
        assert tr.DIGEST_CACHE_KEY not in redis.values

    def test_never_raises_without_redis(self, monkeypatch):
        monkeypatch.setattr(tr, "_redis", lambda: None)
        assert tr.notify_changed() is False

    def test_the_request_list_stays_short(self, redis):
        for _ in range(50):
            tr.notify_changed()
        assert len(redis.lists[tr.REQUEST_KEY]) == 10
