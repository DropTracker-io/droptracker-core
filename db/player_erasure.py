"""Erase a player (and optionally their user account) on request.

For someone who asks to be removed from DropTracker entirely (first case:
ticket 494). One function plans the work (``plan_erasure``) and another
applies it (``apply_erasure``) inside one DB transaction. Redis, files and
Discord messages are separate steps (``purge_redis``, ``purge_files``,
``delete_discord_messages``) because none of them can roll back with the DB.

What happens to each kind of row:

* **Events keep their numbers.** Removing someone must not change a past
  event's standings, so every ``web_event_*`` / ``event_participants`` row
  that names the player moves to a fresh placeholder player: no name the game
  allows (``Removed player #N`` contains ``#``, which no RSN can), no WOM id,
  no account hash, no user, and ``hidden`` set, so the site shows "Hidden
  player" and keeps it off every board. One placeholder per erasure, so two
  removed people in the same event still count separately. Their proof
  screenshots are the player's own data and are cleared.
* **Everything else that is the player's goes**: drops and every other
  submission table, rollups, points, badges, plugin state, and the rows that
  hang off those rows by foreign key (notification posts, splits, moderation,
  loadouts). Tables are found from ``information_schema``, so a new
  player-keyed table is covered without editing this file.
* **The user account goes**, except where a row is a record about someone
  else or about the request itself: actor columns on other people's rows
  (``granted_by``, ``acted_by_user_id``, ...) are set to NULL, and tickets
  (which must have an author) are moved to a shared "Removed user" account
  with the Discord id blanked. The transcript text is kept as the record of
  the request.
* **Billing refuses.** A user with subscription or payment rows is not erased
  by this; those records have their own retention rules.

Nothing here stops the data coming back: a running plugin or a clan's WOM
sync recreates the player. That is a separate decision (see the ticket 494
dev-tracker task).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from sqlalchemy import bindparam, text

#: Tables whose player rows are re-pointed at the placeholder, not deleted.
def is_event_table(table: str) -> bool:
    return table.startswith("web_event_") or table == "event_participants"


#: Columns (beyond FKs onto players) that hold a player id.
PLAYER_ID_COLUMNS = ("player_id", "victim_player_id")
#: Columns that hold a DropTracker user id without an FK.
USER_ID_COLUMNS = ("user_id",)
#: User-owned tables that must never be erased by this tool.
REFUSE_USER_TABLES = (
    "group_subscriptions", "subscription_payments", "user_subscriptions",
    "group_patreon", "api_keys",
)
#: Tables re-pointed at the "Removed user" account rather than nulled.
TICKET_TABLES = ("tickets",)

PLACEHOLDER_PREFIX = "Removed player #"
REMOVED_USER_NAME = "Removed user"

USER_UPLOAD_ROOT = "/store/droptracker/disc/static/assets/img/user-upload"


@dataclass
class Plan:
    player_id: int | None
    user_id: int | None
    player_name: str | None = None
    wom_id: int | None = None
    discord_id: str | None = None
    # (table, column) -> row count
    reassign: dict = field(default_factory=dict)     # event rows -> placeholder
    delete: dict = field(default_factory=dict)       # player's rows to delete
    delete_user: dict = field(default_factory=dict)  # user's rows to delete
    children: dict = field(default_factory=dict)     # FK children of deleted rows
    null_user: dict = field(default_factory=dict)    # actor columns -> NULL
    ticket_user: dict = field(default_factory=dict)  # -> "Removed user"
    nullable_event_user: dict = field(default_factory=dict)
    wom_member_rows: int = 0  # wom_group_member_ids (keyed by WOM id, no FK)
    refusals: list = field(default_factory=list)
    discord_messages: list = field(default_factory=list)  # (channel_id, message_id)
    image_urls: list = field(default_factory=list)

    @property
    def needs_placeholder(self) -> bool:
        return any(self.reassign.values())


# --------------------------------------------------------------------------- #
# Schema discovery
# --------------------------------------------------------------------------- #
def _base_tables_with(s, columns) -> list[tuple[str, str, bool]]:
    rows = s.execute(text(
        "SELECT c.table_name, c.column_name, c.is_nullable = 'YES' "
        "FROM information_schema.columns c JOIN information_schema.tables t "
        "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = DATABASE() AND t.table_type = 'BASE TABLE' "
        "  AND c.column_name IN :cols"
    ).bindparams(bindparam("cols", expanding=True)), {"cols": list(columns)}).all()
    return sorted((r[0], r[1], bool(r[2])) for r in rows)


def _fks_onto(s, table: str, column: str) -> list[tuple[str, str, bool]]:
    rows = s.execute(text(
        "SELECT k.table_name, k.column_name, c.is_nullable = 'YES' "
        "FROM information_schema.key_column_usage k JOIN information_schema.columns c "
        "  ON c.table_schema = k.table_schema AND c.table_name = k.table_name "
        "  AND c.column_name = k.column_name "
        "WHERE k.table_schema = DATABASE() AND k.referenced_table_name = :t "
        "  AND k.referenced_column_name = :c"
    ), {"t": table, "c": column}).all()
    return sorted((r[0], r[1], bool(r[2])) for r in rows)


def _pk(s, table: str) -> str | None:
    rows = s.execute(text(
        "SELECT column_name FROM information_schema.key_column_usage "
        "WHERE table_schema = DATABASE() AND table_name = :t AND constraint_name = 'PRIMARY'"
    ), {"t": table}).scalars().all()
    return rows[0] if len(rows) == 1 else None


def _q(sql: str, seconds: int = 30) -> str:
    return f"SET STATEMENT max_statement_time={seconds} FOR {sql}"


def _count(s, table, col, value) -> int:
    return s.execute(text(_q(f"SELECT COUNT(*) FROM `{table}` WHERE `{col}` = :v")),
                     {"v": value}).scalar()


def player_columns(s) -> list[tuple[str, str, bool]]:
    """Every (table, column, nullable) that holds a player id, ``players``
    itself excluded."""
    cols = set(_fks_onto(s, "players", "player_id"))
    cols |= set(_base_tables_with(s, PLAYER_ID_COLUMNS))
    return sorted(c for c in cols if c[0] != "players")


def user_columns(s) -> list[tuple[str, str, bool]]:
    """Every (table, column, nullable) that holds a user id, ``users`` excluded.
    Non-FK columns are limited to ``USER_ID_COLUMNS`` so an unrelated
    ``created_by`` (or ``xf_user_id``) is never mistaken for one."""
    cols = set(_fks_onto(s, "users", "user_id"))
    cols |= set(_base_tables_with(s, USER_ID_COLUMNS))
    return sorted(c for c in cols if c[0] != "users")


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def plan_erasure(s, player_id: int | None, user_id: int | None,
                 *, keep_owner: bool = False) -> Plan:
    """``keep_owner`` erases a claimed player but leaves the owning user (and
    their other accounts) alone: the self-serve, one-account-at-a-time path."""
    plan = Plan(player_id=player_id, user_id=user_id)

    if player_id is not None:
        p = s.execute(text(
            "SELECT player_name, wom_id, user_id FROM players WHERE player_id = :p"
        ), {"p": player_id}).first()
        if p is None:
            plan.refusals.append(f"player {player_id} does not exist")
            return plan
        plan.player_name, plan.wom_id = p.player_name, p.wom_id
        if p.user_id is not None and p.user_id != user_id and not keep_owner:
            plan.refusals.append(
                f"player is claimed by user {p.user_id}; pass --user {p.user_id} or unclaim first")

        for table, col, _nullable in player_columns(s):
            n = _count(s, table, col, player_id)
            if not n:
                continue
            (plan.reassign if is_event_table(table) else plan.delete)[(table, col)] = n

        if plan.wom_id:
            plan.wom_member_rows = _count(s, "wom_group_member_ids", "player_wom_id", plan.wom_id)
        _plan_children(s, plan)
        _plan_discord_messages(s, plan)
        _plan_images(s, plan)

    if user_id is not None:
        u = s.execute(text("SELECT discord_id FROM users WHERE user_id = :u"),
                      {"u": user_id}).first()
        if u is None:
            plan.refusals.append(f"user {user_id} does not exist")
            return plan
        plan.discord_id = str(u.discord_id) if u.discord_id else None
        others = s.execute(text(
            "SELECT player_id FROM players WHERE user_id = :u AND player_id <> :p"
        ), {"u": user_id, "p": player_id if player_id is not None else -1}).scalars().all()
        if others:
            plan.refusals.append(f"user {user_id} also owns players {others}; erase or unclaim those first")

        for table, col, nullable in user_columns(s):
            n = _count(s, table, col, user_id)
            if not n:
                continue
            if table in REFUSE_USER_TABLES:
                plan.refusals.append(f"{table}.{col} has {n} billing rows for this user")
            elif table in TICKET_TABLES and not nullable:
                plan.ticket_user[(table, col)] = n
            elif is_event_table(table):
                if nullable:
                    plan.nullable_event_user[(table, col)] = n
                else:
                    plan.refusals.append(f"{table}.{col} is NOT NULL; cannot unlink event row")
            elif col == "user_id" or table == "ticket_messages":
                # The user's own rows. ticket_messages.author_user_id is the
                # exception: kept as the transcript, unlinked below.
                if table == "ticket_messages":
                    plan.null_user[(table, col)] = n
                else:
                    plan.delete_user[(table, col)] = n
            elif nullable:
                plan.null_user[(table, col)] = n
            else:
                plan.refusals.append(f"{table}.{col} is NOT NULL and names this user as actor")
    return plan


def _player_row_ids(s, plan: Plan, table: str, pk: str) -> list:
    cols = [c for (t, c) in plan.delete if t == table]
    ids: set = set()
    for col in cols:
        ids |= set(s.execute(text(_q(f"SELECT `{pk}` FROM `{table}` WHERE `{col}` = :p", 60)),
                             {"p": plan.player_id}).scalars().all())
    return sorted(ids)


def _plan_children(s, plan: Plan) -> None:
    """Rows in OTHER tables that point (by FK) at rows being deleted, e.g.
    ``notified.drop_id`` or ``personal_best_loadouts.pb_id``.

    One level. If a child row is itself referenced by another FK row, the
    plan refuses rather than guessing; extend this when the schema needs it.
    """
    for table in sorted({t for (t, _c) in plan.delete}):
        pk = _pk(s, table)
        if not pk:
            continue
        fks = _fks_onto(s, table, pk)
        if not fks:
            continue
        ids = _player_row_ids(s, plan, table, pk)
        if not ids:
            continue
        for ctable, ccol, _n in fks:
            n = _count_in(s, ctable, ccol, ids)
            if not n:
                continue
            plan.children[(ctable, ccol, table)] = n
            cpk = _pk(s, ctable)
            for gtable, gcol, _gn in (_fks_onto(s, ctable, cpk) if cpk else []):
                cids = []
                for chunk in _chunks(ids):
                    cids += s.execute(text(_q(
                        f"SELECT `{cpk}` FROM `{ctable}` WHERE `{ccol}` IN :ids"
                    )).bindparams(bindparam("ids", expanding=True)), {"ids": chunk}).scalars().all()
                if cids and _count_in(s, gtable, gcol, cids):
                    plan.refusals.append(
                        f"{gtable}.{gcol} references {ctable} rows being deleted (2nd level)")


def _chunks(ids, n=1000):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _count_in(s, table, col, ids) -> int:
    total = 0
    for chunk in _chunks(ids):
        total += s.execute(text(_q(f"SELECT COUNT(*) FROM `{table}` WHERE `{col}` IN :ids"))
                           .bindparams(bindparam("ids", expanding=True)), {"ids": chunk}).scalar()
    return total


def _plan_discord_messages(s, plan: Plan) -> None:
    """Posts about the player that the bot can still delete."""
    drop_ids = _player_row_ids(s, plan, "drops", "drop_id") if ("drops", "player_id") in plan.delete else []
    rows = set(s.execute(text(_q(
        "SELECT channel_id, message_id FROM notified WHERE player_id = :p AND message_id IS NOT NULL"
    )), {"p": plan.player_id}).all())
    for chunk in _chunks(drop_ids):
        rows |= set(s.execute(text(_q(
            "SELECT channel_id, message_id FROM notified WHERE drop_id IN :ids AND message_id IS NOT NULL"
        )).bindparams(bindparam("ids", expanding=True)), {"ids": chunk}).all())
    plan.discord_messages = sorted((str(c), str(m)) for c, m in rows if c and m)


def _plan_images(s, plan: Plan) -> None:
    urls = set(s.execute(text(_q(
        "SELECT image_url FROM drops WHERE player_id = :p AND image_url IS NOT NULL", 60
    )), {"p": plan.player_id}).scalars().all())
    urls |= set(s.execute(text(_q(
        "SELECT proof_url FROM web_event_completions WHERE player_id = :p AND proof_url IS NOT NULL"
    )), {"p": plan.player_id}).scalars().all())
    plan.image_urls = sorted(u for u in urls if u)


# --------------------------------------------------------------------------- #
# Export (before apply): every row the plan deletes or moves, as JSON lines
# --------------------------------------------------------------------------- #
def _jsonable(v):
    import datetime as _dt
    import decimal

    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def export_rows(s, plan: Plan, write) -> int:
    """Call ``write(line)`` with one JSON object per row the plan touches:
    ``{"table": ..., "action": "delete"|"reassign"|..., "row": {...}}``.
    Returns the row count. Streams by key so a large player never sits in
    memory whole."""
    import json

    n = 0

    def emit(table, action, rows):
        nonlocal n
        for r in rows:
            write(json.dumps({"table": table, "action": action,
                              "row": {k: _jsonable(v) for k, v in r._mapping.items()}},
                             separators=(",", ":")) + "\n")
            n += 1

    def by_value(table, col, value, action):
        pk = _pk(s, table)
        if not pk:
            emit(table, action, s.execute(text(_q(
                f"SELECT * FROM `{table}` WHERE `{col}` = :v", 120)), {"v": value}).all())
            return
        last = None
        while True:
            where = f"`{col}` = :v" + (f" AND `{pk}` > :last" if last is not None else "")
            rows = s.execute(text(_q(
                f"SELECT * FROM `{table}` WHERE {where} ORDER BY `{pk}` LIMIT 5000", 120)),
                {"v": value, "last": last}).all()
            if not rows:
                return
            emit(table, action, rows)
            last = rows[-1]._mapping[pk]

    pid, uid = plan.player_id, plan.user_id
    if pid is not None:
        emit("players", "delete", s.execute(text(
            "SELECT * FROM players WHERE player_id = :p"), {"p": pid}).all())
        for (table, col) in plan.delete:
            by_value(table, col, pid, "delete")
        for (table, col) in plan.reassign:
            by_value(table, col, pid, "reassign")
        for (ctable, ccol, ptable) in plan.children:
            ids = _player_row_ids(s, plan, ptable, _pk(s, ptable))
            for chunk in _chunks(ids):
                emit(ctable, "delete", s.execute(text(_q(
                    f"SELECT * FROM `{ctable}` WHERE `{ccol}` IN :ids", 120))
                    .bindparams(bindparam("ids", expanding=True)), {"ids": chunk}).all())
        if plan.wom_id:
            by_value("wom_group_member_ids", "player_wom_id", plan.wom_id, "delete")
    if uid is not None:
        emit("users", "delete", s.execute(text(
            "SELECT * FROM users WHERE user_id = :u"), {"u": uid}).all())
        for (table, col) in plan.delete_user:
            by_value(table, col, uid, "delete")
        for (table, col) in list(plan.null_user) + list(plan.nullable_event_user):
            by_value(table, col, uid, "unlink")
        for (table, col) in plan.ticket_user:
            by_value(table, col, uid, "unlink")
    return n


# --------------------------------------------------------------------------- #
# Apply (one transaction; caller commits)
# --------------------------------------------------------------------------- #
def create_placeholder_player(s) -> int:
    """A fresh hidden player that real submissions can never resolve to."""
    n = s.execute(text(
        "SELECT COUNT(*) FROM players WHERE player_name LIKE :p"
    ), {"p": PLACEHOLDER_PREFIX + "%"}).scalar() + 1
    name = f"{PLACEHOLDER_PREFIX}{n}"[:20]
    res = s.execute(text(
        "INSERT INTO players (player_name, user_id, wom_id, account_hash, "
        " hidden, date_added, date_updated) "
        "VALUES (:n, NULL, NULL, NULL, 1, NOW(), NOW())"
    ), {"n": name})
    return res.lastrowid


def removed_user_id(s) -> int:
    """The shared account that keeps records (tickets) whose author left."""
    uid = s.execute(text(
        "SELECT user_id FROM users WHERE username = :n AND discord_id IS NULL ORDER BY user_id LIMIT 1"
    ), {"n": REMOVED_USER_NAME}).scalar()
    if uid is not None:
        return uid
    import secrets
    res = s.execute(text(
        "INSERT INTO users (username, discord_id, auth_token) VALUES (:n, NULL, :t)"
    ), {"n": REMOVED_USER_NAME, "t": secrets.token_hex(8)})
    return res.lastrowid


def apply_erasure(s, plan: Plan) -> dict:
    if plan.refusals:
        raise RuntimeError("refusing: " + "; ".join(plan.refusals))
    done: dict = {}
    pid, uid = plan.player_id, plan.user_id

    if pid is not None:
        if plan.needs_placeholder:
            ph = create_placeholder_player(s)
            done["placeholder_player_id"] = ph
            s.execute(text(
                "UPDATE web_event_completions SET proof_url = NULL WHERE player_id = :p"
            ), {"p": pid})
            for (table, col) in plan.reassign:
                done[f"reassign {table}.{col}"] = s.execute(text(
                    f"UPDATE `{table}` SET `{col}` = :ph WHERE `{col}` = :p"
                ), {"ph": ph, "p": pid}).rowcount

        # FK children of the player's rows go before the rows themselves.
        parent_ids: dict = {}
        for (ctable, ccol, ptable) in plan.children:
            if ptable not in parent_ids:
                pk = _pk(s, ptable)
                parent_ids[ptable] = _player_row_ids(s, plan, ptable, pk)
            n = 0
            for chunk in _chunks(parent_ids[ptable]):
                n += s.execute(text(f"DELETE FROM `{ctable}` WHERE `{ccol}` IN :ids")
                               .bindparams(bindparam("ids", expanding=True)), {"ids": chunk}).rowcount
            done[f"delete {ctable}.{ccol} (child of {ptable})"] = n

    if uid is not None:
        for (table, col) in plan.ticket_user:
            done[f"ticket->removed {table}.{col}"] = s.execute(text(
                f"UPDATE `{table}` SET `{col}` = :r WHERE `{col}` = :u"
            ), {"r": removed_user_id(s), "u": uid}).rowcount
        for (table, col) in list(plan.null_user) + list(plan.nullable_event_user):
            done[f"null {table}.{col}"] = s.execute(text(
                f"UPDATE `{table}` SET `{col}` = NULL WHERE `{col}` = :u"
            ), {"u": uid}).rowcount
        if plan.discord_id:
            done["ticket_messages.author_discord_id blanked"] = s.execute(text(
                "UPDATE ticket_messages SET author_discord_id = '' WHERE author_discord_id = :d"
            ), {"d": plan.discord_id}).rowcount

    # Own rows. Retry passes order RESTRICT FKs between the player's own
    # tables (e.g. notified before drops) without hard-coding them.
    pending = [(tc, pid) for tc in plan.delete] + [(tc, uid) for tc in plan.delete_user]
    for _ in range(6):
        failed = []
        for (table, col), value in pending:
            try:
                with s.begin_nested():
                    n = s.execute(text(f"DELETE FROM `{table}` WHERE `{col}` = :v"),
                                  {"v": value}).rowcount
                done[f"delete {table}.{col}"] = done.get(f"delete {table}.{col}", 0) + n
            except Exception:
                failed.append(((table, col), value))
        if not failed:
            break
        pending = failed
    else:
        raise RuntimeError(f"could not delete (FK order): {[p for p, _ in pending]}")

    if pid is not None and plan.wom_id:
        done["delete wom_group_member_ids"] = s.execute(text(
            "DELETE FROM wom_group_member_ids WHERE player_wom_id = :w"
        ), {"w": plan.wom_id}).rowcount
    if pid is not None:
        done["delete players"] = s.execute(text("DELETE FROM players WHERE player_id = :p"),
                                           {"p": pid}).rowcount
    if uid is not None:
        done["delete users"] = s.execute(text("DELETE FROM users WHERE user_id = :u"),
                                         {"u": uid}).rowcount
    return done


# --------------------------------------------------------------------------- #
# Side stores (after the DB commit)
# --------------------------------------------------------------------------- #
def redis_keys_for(player_id: int | None, user_id: int | None) -> list[str]:
    """Key patterns (SCAN globs) holding the player's / user's own data."""
    pats: list[str] = []
    if player_id is not None:
        p = player_id
        pats += [f"player:{p}", f"player:{p}:*", f"p:{p}", f"pstats:*:{p}:*",
                 f"plugin:notify:{p}", f"rank_watch:*:{p}", f"wom:eventupd:{p}",
                 f"events:*:kcdedupe:*:{p}"]
    if user_id is not None:
        u = user_id
        pats += [f"web:profile:{u}", f"web:guilds:{u}", f"web:guildmeta:{u}",
                 f"web:ratelimit:*:{u}"]
    return pats


def purge_redis(r, player_id: int | None, user_id: int | None, apply: bool) -> dict:
    out = {"keys": 0, "leaderboard_members": 0}
    keys: list = []
    for pat in redis_keys_for(player_id, user_id):
        if any(ch in pat for ch in "*?["):
            keys += list(r.scan_iter(match=pat, count=5000))
        elif r.exists(pat):
            keys.append(pat)
    out["keys"] = len(keys)
    boards = []
    if player_id is not None:
        for k in r.scan_iter(match="leaderboard:*", count=5000):
            if r.type(k) in (b"zset", "zset") and r.zscore(k, str(player_id)) is not None:
                boards.append(k)
    out["leaderboard_members"] = len(boards)
    if apply:
        for i in range(0, len(keys), 500):
            r.delete(*keys[i:i + 500])
        for k in boards:
            r.zrem(k, str(player_id))
    return out


def purge_files(plan: Plan, apply: bool) -> dict:
    """The player's upload tree, on B2 and locally, plus any stray image_url.

    B2 goes first: the local tree is owned by the intake service account, so
    a run as another user can fail there, and that must not leave the bucket
    copies behind. Local failures are reported (``local_error``), not raised.
    """
    out = {"local_dir": None, "local_files": 0, "b2_keys": 0}
    keys: set = set()
    try:
        from scripts.prune_drop_images import b2_key_for, local_path_for, _b2_storage
        b2 = _b2_storage()
        if b2 is not None and plan.wom_id:
            for obj in b2.list_keys(f"{b2.USER_UPLOAD_PREFIX}/{plan.wom_id}/"):
                keys.add(obj["key"] if isinstance(obj, dict) else obj)
        stray = []
        for url in plan.image_urls:
            k = b2_key_for(url)
            if k:
                keys.add(k)
            path = local_path_for(url)
            if path and os.path.isfile(path):
                stray.append(path)
        out["b2_keys"] = len(keys)
        if apply and keys and b2 is not None:
            b2.delete_keys(sorted(keys))
    except Exception as e:  # report, don't hide
        out["b2_error"] = f"{type(e).__name__}: {e}"
        stray = []

    if plan.wom_id:
        d = os.path.join(USER_UPLOAD_ROOT, str(plan.wom_id))
        if os.path.isdir(d):
            out["local_dir"] = d
            out["local_files"] = sum(len(f) for _r, _d, f in os.walk(d))
    if apply:
        try:
            for path in stray:
                os.unlink(path)
            if out["local_dir"]:
                shutil.rmtree(out["local_dir"])
        except OSError as e:
            out["local_error"] = (f"{type(e).__name__}: {e} (the tree is owned by the "
                                  "intake service account; remove it as that user)")
    return out
