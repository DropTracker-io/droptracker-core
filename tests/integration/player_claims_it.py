"""Integration scenario for the shared RSN claim service (db/player_claims.py).

Run standalone (NOT collected by pytest — see test_player_claims_db.py which
invokes it as a subprocess so the unit-test sys.modules stubs never apply):

    cd /store/droptracker/disc && venv/bin/python tests/integration/player_claims_it.py

The claim service runs on the global scoped session (its mutators — e.g.
``Player.add_group`` — commit there), so this script REBINDS that scoped
session to ``dt_migrate_test`` before touching the service, and cleans up the
committed rows explicitly in a finally block (salted identifiers).
"""
import configparser
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

    # Rebind the global scoped session BEFORE importing the service so every
    # commit it makes lands in dt_migrate_test, never the live `data` schema.
    from db.models import session as global_session
    global_session.remove()
    global_session.configure(bind=engine)

    from db.models import Group, Guild, Player, User, user_group_association
    from db import player_claims as pc

    salt = os.getpid()
    guild_id = str(810_000_000 + salt)
    owner_discord = str(820_000_000 + salt)
    rival_discord = str(830_000_000 + salt)
    rsn = f"ClaimIt{salt}"

    s = global_session
    created_group_ids = []
    created_user_ids = []
    created_player_ids = []
    try:
        # --- Fixture: a guild-linked group and one unclaimed player ---
        group = Group(f"ClaimIt G{salt}"[:30], 84_000_000 + salt, guild_id)
        s.add(group)
        s.commit()
        created_group_ids.append(group.group_id)
        guild = Guild(guild_id=guild_id, group_id=group.group_id)
        s.add(guild)
        player = Player(
            wom_id=85_000_000 + salt,
            player_name=rsn,
            account_hash=f"claim-it-{salt}",
        )
        s.add(player)
        s.commit()
        created_player_ids.append(player.player_id)

        # --- ensure_user_provisioned creates a user, race-safe on re-entry ---
        user = pc.ensure_user_provisioned(owner_discord, "Claim It Tester")
        assert user is not None and user.discord_id == owner_discord
        created_user_ids.append(user.user_id)
        again = pc.ensure_user_provisioned(owner_discord, "Claim It Tester")
        assert again.user_id == user.user_id, "second call must return the same user"

        # --- preview: unclaimed player in guild context ---
        prev = pc.preview_claim(rsn, discord_id=owner_discord, guild_id=guild_id)
        assert prev["status"] == "claimable", prev
        assert prev["player_id"] == player.player_id
        assert prev["group_id"] == group.group_id, "guild context must resolve the guild group"

        # --- preview: unknown player ---
        assert pc.preview_claim(f"Nobody{salt}")["status"] == "not_found"

        # --- claim happy path (case-insensitive lookup, guild group attach) ---
        res = pc.claim_player(rsn.lower(), discord_id=owner_discord, guild_id=guild_id)
        assert res["status"] == "claimed", res
        assert res["group_id"] == group.group_id
        s.expire_all()
        p = s.query(Player).filter(Player.player_id == player.player_id).first()
        assert p.user_id == user.user_id, "claim must link players.user_id"
        assoc = s.query(user_group_association).filter_by(
            player_id=player.player_id, group_id=group.group_id).first()
        assert assoc is not None, "claim must attach the player to the guild group"

        # --- already_yours / claimed_by_other ---
        assert pc.claim_player(rsn, discord_id=owner_discord)["status"] == "already_yours"
        rival = pc.claim_player(rsn, discord_id=rival_discord, username="Rival")
        assert rival["status"] == "claimed_by_other", rival
        assert rival["owner_discord_id"] == owner_discord
        created_user_ids.append(
            s.query(User.user_id).filter(User.discord_id == rival_discord).scalar()
        )
        prev2 = pc.preview_claim(rsn, discord_id=rival_discord)
        assert prev2["status"] == "claimed_by_other"
        prev3 = pc.preview_claim(rsn, discord_id=owner_discord)
        assert prev3["status"] == "already_yours"

        # --- unclaim: ownership gate, group removal, user_id cleared ---
        not_yours = pc.unclaim_player(discord_id=rival_discord, player_id=player.player_id)
        assert not_yours["status"] == "not_yours"
        ok = pc.unclaim_player(discord_id=owner_discord, player_id=player.player_id)
        assert ok["status"] == "unclaimed", ok
        s.expire_all()
        p = s.query(Player).filter(Player.player_id == player.player_id).first()
        assert p.user_id is None, "unclaim must clear players.user_id"
        assoc = s.query(user_group_association).filter_by(
            player_id=player.player_id, group_id=group.group_id).first()
        assert assoc is None, "unclaim must remove group associations"

        # --- unclaim of an unknown player ---
        assert pc.unclaim_player(discord_id=owner_discord, player_id=999_999_999)[
            "status"] == "not_found"

        print("ALL PLAYER CLAIM INTEGRATION ASSERTIONS PASSED")
    finally:
        # The service commits as it goes — clean up explicitly. All ids are
        # integers this script generated, so inline formatting is safe.
        try:
            s.rollback()

            def _in(ids):
                return ",".join(str(int(i)) for i in ids)

            uid_list = [u for u in created_user_ids if u]
            with engine.begin() as conn:
                if created_player_ids:
                    pids = _in(created_player_ids)
                    conn.execute(text(
                        f"DELETE FROM user_group_association WHERE player_id IN ({pids})"))
                    conn.execute(text(f"DELETE FROM players WHERE player_id IN ({pids})"))
                if uid_list:
                    uids = _in(uid_list)
                    conn.execute(text(
                        f"DELETE FROM user_group_association WHERE user_id IN ({uids})"))
                    conn.execute(text(
                        f"DELETE FROM user_configurations WHERE user_id IN ({uids})"))
                    conn.execute(text(f"DELETE FROM users WHERE user_id IN ({uids})"))
                conn.execute(text("DELETE FROM guilds WHERE guild_id = :gid"),
                             {"gid": guild_id})
                if created_group_ids:
                    gids = _in(created_group_ids)
                    conn.execute(text(
                        f"DELETE FROM group_configurations WHERE group_id IN ({gids})"))
                    conn.execute(text(f"DELETE FROM groups WHERE group_id IN ({gids})"))
        finally:
            global_session.remove()


if __name__ == "__main__":
    main()
