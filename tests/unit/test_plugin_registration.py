"""Registering a group from the RuneLite plugin (services/plugin_registration.py).

What these pin:

* a registration is checked before anything is held: a name, and either a
  WiseOldMan id or a clan roster to make a WOM group from;
* only the account that started it can see its status, and the group-admin
  session is handed over once;
* the roster is cleaned to valid, distinct RSNs and clan titles map to WOM roles;
* completing creates the WOM group when asked, creates the group in the
  leader's server, claims the game account and mints the admin session; a
  second call (a page refresh) changes nothing; an account owned by someone
  else gets the group but no session.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("_real_plugin_registration",
                                               _ROOT / "services" / "plugin_registration.py")
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)


class FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        value = self.data.get(key)
        return value.encode() if isinstance(value, str) else value

    def set(self, key, value, ex=None):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class RenameError(Exception):
    pass


def _normalize(raw):
    name = str(raw or "").strip()
    if not name or len(name) > 30:
        raise RenameError("Group name must be 1-30 characters.")
    return name


@pytest.fixture(autouse=True)
def rename(monkeypatch):
    monkeypatch.setitem(sys.modules, "db.group_rename",
                        SimpleNamespace(GroupRenameError=RenameError, normalize_group_name=_normalize))


ROSTER = [{"name": "Zezima", "rank": "Owner"}, {"name": "Lynx Titan", "rank": "Deputy Owner"}]


def _start(rc, **over):
    args = dict(acc_hash="-900", player_name="Zezima", name="Iron Foundry", wom_id=None, roster=ROSTER)
    args.update(over)
    return reg.start(rc, **args)


def test_a_name_and_a_wom_group_or_roster_are_needed():
    rc = FakeRedis()
    with pytest.raises(reg.RegistrationError) as e:
        _start(rc, name="  ")
    assert e.value.field == "name"
    with pytest.raises(reg.RegistrationError) as e:
        _start(rc, roster=[])
    assert e.value.field == "wom" and "clan settings" in e.value.message
    with pytest.raises(reg.RegistrationError):
        _start(rc, wom_id="abc")
    assert rc.data == {}


def test_start_holds_it_and_links_to_the_sign_in_with_the_bot():
    rc = FakeRedis()
    out = _start(rc, wom_id=1234, roster=ROSTER)
    assert reg.is_id(out["id"]) and out["expires_in"] == reg.TTL_SECONDS
    assert out["path"] == f"/api/auth/login?bot=1&redirect=/register-group/{out['id']}"
    held = json.loads(rc.data[f"plugreg:{out['id']}"])
    # An existing WOM group needs no roster, so none is kept.
    assert held["wom_id"] == 1234 and held["roster"] == [] and held["status"] == "pending"
    assert reg.preview(rc, out["id"]) == {"name": "Iron Foundry", "status": "pending", "group_id": None,
                                          "creates_wom": False, "members": 0}


def test_starting_again_replaces_the_accounts_previous_registration():
    rc = FakeRedis()
    first = _start(rc)["id"]
    second = _start(rc)["id"]
    assert f"plugreg:{first}" not in rc.data and f"plugreg:{second}" in rc.data


def test_status_is_only_for_the_starting_account_and_the_session_is_handed_over_once():
    rc = FakeRedis()
    reg_id = _start(rc)["id"]
    assert reg.status(rc, reg_id, "-999") == {"status": "failed", "message": reg.EXPIRED}
    assert reg.status(rc, reg_id, "-900")["status"] == "pending"
    held = json.loads(rc.data[f"plugreg:{reg_id}"])
    held.update(status="done", group_id=7, group_name="Iron Foundry", token="dtp_secret")
    rc.set(f"plugreg:{reg_id}", json.dumps(held))
    assert reg.status(rc, reg_id, "-900")["token"] == "dtp_secret"
    assert reg.status(rc, reg_id, "-900")["token"] is None
    assert reg.status(rc, "not an id", "-900")["status"] == "failed"


def test_the_roster_is_cleaned_to_distinct_valid_names():
    roster = [{"name": "Lynx Titan", "rank": "Owner"}, {"name": "lynx_titan"}, {"name": "x" * 13},
              {"name": "<b>bad</b>"}, "not a dict", {"name": "Woox", "rank": None}]
    assert reg.clean_roster(roster) == [{"name": "Lynx Titan", "rank": "Owner"}, {"name": "Woox", "rank": ""}]
    assert reg.clean_roster(None) == []


@pytest.mark.parametrize("title,role", [("Owner", "owner"), ("Deputy Owner", "deputy_owner"),
                                        ("Administrator", "administrator"), ("Made-up title", "member"),
                                        ("", "member")])
def test_clan_titles_map_to_wom_roles(title, role):
    assert reg.wom_role(title) == role


# --------------------------------------------------------------------------- #
# complete
# --------------------------------------------------------------------------- #
class FakeSession:
    def __init__(self, player):
        self.player, self.added, self.commits = player, [], 0

    def query(self, *a):
        return self

    def filter(self, *a):
        return self

    def first(self):
        return self.player

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


@pytest.fixture()
def wired(monkeypatch):
    def make(player_owner=None, create_status="created"):
        state = {"created": [], "claimed": [], "minted": [], "wom": []}
        player = SimpleNamespace(player_name="Zezima", user_id=player_owner)
        session = FakeSession(player)

        async def create_web_group(**kw):
            state["created"].append(kw)
            if create_status != "created":
                return {"success": False, "status": create_status}
            return {"success": True, "status": "created", "group_id": 42, "group_name": kw["group_name"]}

        def claim(rsn, **kw):
            state["claimed"].append((rsn, kw))
            player.user_id = 5
            return {"status": "claimed"}

        async def make_wom(name, roster):
            state["wom"].append((name, roster))
            return 9876, "123-456-789"

        models = SimpleNamespace(AuditLog=lambda **kw: ("audit", kw), Player=SimpleNamespace(account_hash=None),
                                 GroupConfiguration=lambda **kw: ("config", kw), Session=lambda: session)
        monkeypatch.setitem(sys.modules, "db.models", models)
        monkeypatch.setitem(sys.modules, "db.group_creation", SimpleNamespace(create_web_group=create_web_group))
        monkeypatch.setitem(sys.modules, "db.player_claims", SimpleNamespace(claim_player=claim))
        sessions = SimpleNamespace(
            current_generation=lambda s, gid: 1,
            mint=lambda s, gid, uid, acc, gen: state["minted"].append((gid, uid, acc, gen)) or "dtp_new")
        codes = SimpleNamespace(issue_code=lambda s, gid, uid, via: "111-222-333")
        monkeypatch.setattr(sys.modules["db"], "plugin_admin_sessions", sessions, raising=False)
        monkeypatch.setattr(sys.modules["db"], "group_verification_codes", codes, raising=False)
        monkeypatch.setitem(sys.modules, "db.plugin_admin_sessions", sessions)
        monkeypatch.setitem(sys.modules, "db.group_verification_codes", codes)
        monkeypatch.setattr(reg, "_create_wom_group", make_wom)
        state["session"] = session
        return state
    return make


def _complete(rc, reg_id):
    return asyncio.run(reg.complete(rc, reg_id, user_id=5, discord_id="111", username="zez", guild_id="555"))


def test_complete_makes_the_wom_group_creates_the_group_and_hands_the_plugin_a_session(wired):
    state = wired()
    rc = FakeRedis()
    reg_id = _start(rc)["id"]
    out = _complete(rc, reg_id)
    assert out == {"group_id": 42, "group_name": "Iron Foundry", "verification_code": "111-222-333"}
    assert state["wom"] == [("Iron Foundry", ROSTER)]
    assert state["created"][0]["wom_id"] == 9876 and state["created"][0]["guild_id"] == "555"
    assert state["created"][0]["created_via"] == "plugin"
    assert ("config", {"group_id": 42, "config_key": "wom_verification_code",
                       "config_value": "123-456-789"}) in state["session"].added
    assert state["claimed"][0][0] == "Zezima"
    assert state["minted"] == [(42, 5, "-900", 1)]
    done = reg.status(rc, reg_id, "-900")
    assert done["status"] == "done" and done["token"] == "dtp_new" and done["group_id"] == 42

    # A refresh of the website page changes nothing.
    again = _complete(rc, reg_id)
    assert again["group_id"] == 42 and again["verification_code"] is None and len(state["created"]) == 1


def test_an_existing_wom_group_is_used_as_it_is(wired):
    state = wired()
    rc = FakeRedis()
    _complete(rc, _start(rc, wom_id=1234)["id"])
    assert state["wom"] == [] and state["created"][0]["wom_id"] == 1234


def test_an_account_owned_by_someone_else_gets_the_group_but_no_session(wired):
    state = wired(player_owner=99)
    rc = FakeRedis()
    reg_id = _start(rc)["id"]
    _complete(rc, reg_id)
    assert state["claimed"] == [] and state["minted"] == []
    done = reg.status(rc, reg_id, "-900")
    assert done["token"] is None and "droptracker.io" in done["message"]


def test_a_server_that_already_has_a_group_is_refused(wired):
    wired(create_status="guild_conflict")
    rc = FakeRedis()
    with pytest.raises(reg.RegistrationError) as e:
        _complete(rc, _start(rc, wom_id=1234)["id"])
    assert "already has" in e.value.message
    with pytest.raises(reg.RegistrationError):
        _complete(FakeRedis(), "Zz" * 12)
