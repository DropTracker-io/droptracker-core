"""The phantom "Member Added" burst, and the member count that counted users.

2026-09-12, Frontier (WOM 5206) and 37 other clans: the hourly WOM membership
sync announced "Member Added" for players who had been in the group for months,
and the "Total members" field on those embeds read 464 for a 365-player group.

Two independent bugs, both locked in here.

1. ``user_group_association`` carries BOTH player rows (``player_id`` set,
   ``user_id`` NULL) and Discord-user rows (the other way round). ``notify_group``
   counted the table with a bare ``COUNT(*)``, so every group's linked users were
   added to its member count — 299 groups were overstated.

2. ``Group.players`` is ``lazy='dynamic'``. The core bot shares ONE thread-local
   scoped session across every coroutine, so a task that ends its work with
   ``session.remove()``/``close()`` detaches the objects other in-flight
   coroutines are holding. On a DETACHED instance an AppenderQuery yields
   NOTHING and only warns — it does not raise. ``_sync_group_from_wom`` holds
   its Group across the WOM call and across every Discord send, so a concurrent
   teardown made ``group.players`` return an empty roster mid-sync and every
   member looked like a new join. Nothing was actually written (``add_group``
   re-checks the association itself), which is why the member count never moved
   while the embeds poured out.

The fix reads membership through the association table keyed by a plain int
group_id, and gates the announcement on ``add_group`` reporting a real write.

Like the other model-level tests here (the whole unit suite stubs ``db``), this
builds a miniature model set that mirrors the real relationship configuration
rather than importing the production mappers.
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import (Column, ForeignKey, Integer, String, Table, create_engine,
                        func, select)
from sqlalchemy.orm import declarative_base, relationship, scoped_session, sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]


def _source(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text()


def _function(src: str, name: str):
    """The AST of the first top-level-or-method function called `name`."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _dotted(node) -> str:
    """"a.b.c" for an Attribute/Name chain, else ""."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _calls(tree) -> set:
    """Every dotted call name made in `tree`. Comments and docstrings can't
    fake a match, which matters here — the fixed code documents the banned
    call by name."""
    return {_dotted(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)} - {""}


def _attribute_reads(tree) -> set:
    return {_dotted(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)} - {""}


def _string_constants(tree) -> list:
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


@pytest.fixture
def roster():
    """A group with 4 players, 2 of whom have linked Discord users.

    Mirrors production: one association table, a dynamic ``Group.players`` and
    an overlapping ``Group.users``.
    """
    Base = declarative_base()

    user_group_association = Table(
        "user_group_association", Base.metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("player_id", Integer, ForeignKey("players.player_id"), nullable=True),
        Column("user_id", Integer, ForeignKey("users.user_id"), nullable=True),
        Column("group_id", Integer, ForeignKey("groups.group_id"), nullable=False),
    )

    class Group(Base):
        __tablename__ = "groups"
        group_id = Column(Integer, primary_key=True)
        group_name = Column(String(30))
        players = relationship("Player", secondary=user_group_association,
                               back_populates="groups", overlaps="groups",
                               lazy="dynamic")
        users = relationship("User", secondary=user_group_association,
                             back_populates="groups", overlaps="groups,players")

    class Player(Base):
        __tablename__ = "players"
        player_id = Column(Integer, primary_key=True)
        player_name = Column(String(20))
        wom_id = Column(Integer)
        groups = relationship("Group", secondary=user_group_association,
                              back_populates="players", overlaps="players,users")

    class User(Base):
        __tablename__ = "users"
        user_id = Column(Integer, primary_key=True)
        groups = relationship("Group", secondary=user_group_association,
                              back_populates="users", overlaps="players,groups")

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    scoped = scoped_session(sessionmaker(bind=engine))

    group = Group(group_id=190, group_name="Frontier")
    scoped.add(group)
    for pid, wom in ((1, 8072), (2, 9296), (3, 22970), (4, 30318)):
        p = Player(player_id=pid, player_name=f"p{pid}", wom_id=wom)
        p.groups.append(group)
        scoped.add(p)
    for uid in (11, 12):
        u = User(user_id=uid)
        u.groups.append(group)
        scoped.add(u)
    scoped.commit()

    yield scoped, group, user_group_association, Player
    scoped.remove()
    engine.dispose()


def _count_all_rows(sess, assoc, group_id):
    """What notify_group used to do."""
    return sess.execute(
        select(func.count()).select_from(assoc).where(assoc.c.group_id == group_id)
    ).scalar()


def _count_players(sess, assoc, group_id):
    """What get_player_count does now."""
    return sess.execute(
        select(func.count()).select_from(assoc).where(
            assoc.c.group_id == group_id, assoc.c.player_id.isnot(None))
    ).scalar()


def _member_player_ids(sess, assoc, group_id):
    """The roster read _sync_group_from_wom uses now."""
    return {row[0] for row in sess.execute(
        select(assoc.c.player_id).where(
            assoc.c.group_id == group_id, assoc.c.player_id.isnot(None))
    ).all()}


class TestMemberCount:
    def test_bare_count_star_counts_linked_users_as_members(self, roster):
        # The shipped bug: 4 players + 2 linked users read as 6 members.
        sess, group, assoc, _ = roster
        assert _count_all_rows(sess, assoc, group.group_id) == 6

    def test_player_scoped_count_reports_the_real_roster(self, roster):
        sess, group, assoc, _ = roster
        assert _count_players(sess, assoc, group.group_id) == 4

    def test_the_two_disagree_by_the_number_of_linked_users(self, roster):
        # Frontier: 462 vs 365, i.e. its 97 linked users.
        sess, group, assoc, _ = roster
        assert (_count_all_rows(sess, assoc, group.group_id)
                - _count_players(sess, assoc, group.group_id)) == 2


class TestDetachedGroupRoster:
    def test_a_detached_dynamic_relationship_yields_nothing_and_does_not_raise(self, roster):
        """The trap. If SQLAlchemy ever promotes this to an error, this test
        fails and the guard below can be relaxed — until then, never read a
        roster through `group.players`."""
        sess, group, _assoc, _ = roster
        assert len(list(group.players)) == 4

        sess.remove()  # what a concurrent coroutine's cleanup does

        with pytest.warns(Warning, match="detached"):
            assert list(group.players) == []

    def test_the_association_read_survives_a_concurrent_session_teardown(self, roster):
        sess, group, assoc, _ = roster
        group_db_id = int(group.group_id)  # captured before any await

        sess.remove()

        assert _member_player_ids(sess, assoc, group_db_id) == {1, 2, 3, 4}
        assert _count_players(sess, assoc, group_db_id) == 4

    def test_reading_the_roster_off_a_detached_group_makes_every_member_look_new(self, roster):
        """The exact production symptom, reproduced: 4 phantom joins."""
        sess, group, assoc, Player = roster
        roster_wom_ids = [8072, 9296, 22970, 30318]
        sess.remove()

        candidates = sess.query(Player).filter(Player.wom_id.in_(roster_wom_ids)).all()

        with pytest.warns(Warning, match="detached"):
            stale = {p.player_id for p in group.players}
        assert [p.player_id for p in candidates if p.player_id not in stale] == [1, 2, 3, 4]

        fresh = _member_player_ids(sess, assoc, 190)
        assert [p.player_id for p in candidates if p.player_id not in fresh] == []


class TestProductionSourceGuards:
    """The mini-model above proves the mechanism; these pin the real modules.

    Cheap source-level guards, in the style of the other parity tests here —
    they cost nothing and each one names a regression that actually shipped.
    """

    def test_add_group_and_remove_group_report_whether_they_wrote(self):
        src = _source("db/models/player.py")
        for name in ("add_group", "remove_group"):
            returned = {ast.dump(n.value) for n in ast.walk(_function(src, name))
                        if isinstance(n, ast.Return) and n.value is not None}
            assert returned == {ast.dump(ast.Constant(True)), ast.dump(ast.Constant(False))}, (
                f"Player.{name} must report whether it wrote a membership row — "
                "callers gate the join/leave announcement on it")

    def test_the_sync_never_reads_the_roster_through_the_dynamic_relationship(self):
        reads = _attribute_reads(_function(_source("db/ops.py"), "_sync_group_from_wom"))
        assert "group.players" not in reads, (
            "group.players yields nothing on a detached instance — read "
            "membership from user_group_association keyed by group_id instead")

    def test_the_join_announcement_is_gated_on_a_real_write(self):
        fn = _function(_source("db/ops.py"), "_sync_group_from_wom")
        assert "member.add_group" in _calls(fn)
        assigned = {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)
                    and isinstance(n.value, ast.Call)
                    and _dotted(n.value.func) == "member.add_group"}
        assert assigned, "add_group's return value must be captured, not discarded"
        tested = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
                  and isinstance(n.ctx, ast.Load)}
        assert assigned & tested, (
            "announcing without checking add_group's return is what produced "
            "'Member Added' embeds with nothing behind them")

    def test_the_member_count_embed_does_not_count_user_rows(self):
        fn = _function(_source("db/ops.py"), "notify_group")
        sql = " ".join(_string_constants(fn)).upper()
        assert "COUNT(*) FROM USER_GROUP_ASSOCIATION" not in sql, (
            "that counts the group's linked Discord users as members too")
        # once for the join embed, once for the leave embed
        assert sum(c.endswith("get_player_count") for c in _calls(fn)) == 1
        assert sum(1 for n in ast.walk(fn) if isinstance(n, ast.Call)
                   and _dotted(n.func).endswith("get_player_count")) == 2

    @pytest.mark.parametrize("path", [
        "services/channel_names.py",
        "commands/user.py",
        "commands/submissions.py",
    ])
    def test_background_work_never_tears_down_the_shared_scoped_session(self, path):
        calls = _calls(ast.parse(_source(path)))
        assert "session.remove" not in calls, (
            f"{path}: session.remove() on the shared scoped session detaches "
            "objects other coroutines hold across an await. Use a private "
            "Session (db.models.db_session) or session.expire_all().")
