"""Integration scenario for the shared group-creation service (db/group_creation.py).

Run standalone (NOT collected by pytest — see test_group_creation_db.py which
invokes it as a subprocess so the unit-test sys.modules stubs never apply):

    cd /store/droptracker/disc && venv/bin/python tests/integration/group_creation_it.py

Rebinds the global scoped session to ``dt_migrate_test`` (the service commits
there), patches the external side effects (XenForo mirror, realtime ticker,
WOM sync) to recorders, and cleans up committed rows in a finally block.

Covers the 2026-07 harmonization: group_admins owner seeding on every path,
``invalid_name`` validation, and the in-service initial WOM sync scheduling.
"""
import asyncio
import configparser
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from dotenv import dotenv_values  # noqa: E402
_env = dotenv_values(os.path.join(ROOT, ".env"))
for _k in ("DB_USER", "DB_PASS"):
    if _env.get(_k):
        os.environ[_k] = _env[_k]

from sqlalchemy import create_engine, text  # noqa: E402

TEST_DB = "dt_migrate_test"


def _test_engine():
    ini = configparser.ConfigParser()
    ini.read(os.path.join(ROOT, "alembic.ini"))
    base, _, _db = ini.get("alembic", "sqlalchemy.url").rpartition("/")
    assert TEST_DB != "data"
    return create_engine(f"{base}/{TEST_DB}")


def main():
    engine = _test_engine()
    assert engine.url.database == TEST_DB, f"refusing to run against {engine.url.database}"

    from db.models import session as global_session
    global_session.remove()
    global_session.configure(bind=engine)

    from db.models import (
        Group, GroupAdmin, GroupConfiguration, Guild, User, user_group_association,
    )
    from db import group_creation as gc

    # --- Patch external side effects to recorders (no XF/Redis/WOM traffic) ---
    calls = {"xf": 0, "ticker": 0, "wom_sync": []}

    async def _fake_xf(group):
        calls["xf"] += 1

    async def _fake_wom_sync(wom_id):
        calls["wom_sync"].append(int(wom_id))

    gc.insert_xf_group = _fake_xf
    gc._run_initial_wom_sync = _fake_wom_sync

    import services.realtime as rt

    def _fake_ticker(group_id, name):
        calls["ticker"] += 1

    rt.publish_feed_group_created = _fake_ticker

    salt = os.getpid()
    guild_id = str(910_000_000 + salt)
    other_guild_id = str(915_000_000 + salt)
    owner_discord = str(920_000_000 + salt)
    wom_id = 93_000_000 + salt
    name = f"GC It {salt}"[:30]

    s = global_session
    created_group_ids = []
    created_user_ids = []
    try:
        async def scenario():
            # --- invalid_name: empty + over the 30-char column cap ---
            r = await gc.create_web_group(
                group_name="", wom_id=wom_id, guild_id=guild_id,
                owner_discord_id=owner_discord)
            assert r["status"] == "invalid_name", r
            r = await gc.create_web_group(
                group_name="X" * 31, wom_id=wom_id, guild_id=guild_id,
                owner_discord_id=owner_discord)
            assert r["status"] == "invalid_name", r
            assert s.query(Guild).filter(Guild.guild_id == guild_id).first() is None, \
                "invalid_name must not create anything"

            # --- created: full side-effect audit ---
            r = await gc.create_web_group(
                group_name=name, wom_id=wom_id, guild_id=guild_id,
                owner_discord_id=owner_discord, owner_username="GC Tester")
            assert r["status"] == "created", r
            assert r["group_name"] == name
            gid = r["group_id"]
            created_group_ids.append(gid)
            await asyncio.sleep(0)  # let the scheduled sync task run

            user = s.query(User).filter(User.discord_id == owner_discord).first()
            assert user is not None
            created_user_ids.append(user.user_id)

            guild = s.query(Guild).filter(Guild.guild_id == guild_id).first()
            assert guild is not None and guild.group_id == gid, "guild must link to the group"

            # Harmonization: the creator is seeded as group_admins owner on
            # EVERY path (previously web-route-only).
            ga = (s.query(GroupAdmin)
                  .filter(GroupAdmin.group_id == gid, GroupAdmin.user_id == user.user_id)
                  .first())
            assert ga is not None and ga.role == "owner", "creator must be seeded as owner"

            # Config clone: an export key is always minted; the overridden
            # template keys only exist when template group 1 has rows (the
            # test schema's template is empty, prod's is not).
            cfg = {c.config_key: c.config_value for c in
                   s.query(GroupConfiguration).filter(GroupConfiguration.group_id == gid)}
            assert cfg.get("export_api_key"), "export_api_key must always be minted"
            if "clan_name" in cfg:
                assert cfg["clan_name"] == name
            if "authed_users" in cfg:
                assert json.loads(cfg["authed_users"]) == [owner_discord]

            # Owner membership association.
            assoc = s.query(user_group_association).filter_by(
                user_id=user.user_id, group_id=gid).first()
            assert assoc is not None, "creator must be a member of the new group"

            # External side effects fired.
            assert calls["xf"] == 1 and calls["ticker"] == 1
            assert calls["wom_sync"] == [wom_id], "initial WOM sync must be scheduled"

            # --- already_registered (same guild + same wom) ---
            r = await gc.create_web_group(
                group_name=name, wom_id=wom_id, guild_id=guild_id,
                owner_discord_id=owner_discord)
            assert r["status"] == "already_registered", r

            # --- guild_conflict (same guild, different wom) ---
            r = await gc.create_web_group(
                group_name=name, wom_id=wom_id + 1, guild_id=guild_id,
                owner_discord_id=owner_discord)
            assert r["status"] == "guild_conflict", r

            # --- wom_conflict (different guild, same wom) ---
            r = await gc.create_web_group(
                group_name=name, wom_id=wom_id, guild_id=other_guild_id,
                owner_discord_id=owner_discord)
            assert r["status"] == "wom_conflict", r

            # --- invalid_wom ---
            r = await gc.create_web_group(
                group_name=name, wom_id="abc", guild_id=other_guild_id,
                owner_discord_id=owner_discord)
            assert r["status"] == "invalid_wom", r

            # --- initial_sync=False skips scheduling ---
            calls["wom_sync"].clear()
            r = await gc.create_web_group(
                group_name=f"GC It2 {salt}"[:30], wom_id=wom_id + 2,
                guild_id=other_guild_id, owner_discord_id=owner_discord,
                initial_sync=False)
            assert r["status"] == "created", r
            created_group_ids.append(r["group_id"])
            await asyncio.sleep(0)
            assert calls["wom_sync"] == [], "initial_sync=False must not schedule a sync"

        asyncio.run(scenario())
        print("ALL GROUP CREATION INTEGRATION ASSERTIONS PASSED")
    finally:
        try:
            s.rollback()

            def _in(ids):
                return ",".join(str(int(i)) for i in ids)

            uid_list = [u for u in created_user_ids if u]
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM guilds WHERE guild_id IN (:a, :b)"),
                             {"a": guild_id, "b": other_guild_id})
                if created_group_ids:
                    gids = _in(created_group_ids)
                    conn.execute(text(
                        f"DELETE FROM group_admins WHERE group_id IN ({gids})"))
                    conn.execute(text(
                        f"DELETE FROM group_configurations WHERE group_id IN ({gids})"))
                    conn.execute(text(
                        f"DELETE FROM user_group_association WHERE group_id IN ({gids})"))
                    conn.execute(text(f"DELETE FROM groups WHERE group_id IN ({gids})"))
                if uid_list:
                    uids = _in(uid_list)
                    conn.execute(text(
                        f"DELETE FROM user_group_association WHERE user_id IN ({uids})"))
                    conn.execute(text(
                        f"DELETE FROM user_configurations WHERE user_id IN ({uids})"))
                    conn.execute(text(f"DELETE FROM users WHERE user_id IN ({uids})"))
        finally:
            global_session.remove()


if __name__ == "__main__":
    main()
