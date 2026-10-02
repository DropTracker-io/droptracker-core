"""utils/catalog_audit: every new items / npc_list row records where it came
from (ticket #456: two malformed rows nobody could trace)."""

import json
import traceback

import pytest
from sqlalchemy import Column, Integer, String, Text, create_engine, select
from sqlalchemy.orm import declarative_base, sessionmaker

from utils import catalog_audit as ca


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_submission_context_keeps_provenance_fields_only():
    ctx = ca.submission_context(
        {"type": "drop", "player": "Chief McD", "guid": "abc", "used_api": True,
         "npc_name": "NPC Reward pool (Tempoross)", "acc_hash": "secret",
         "image_url": "", "value": 0},
        "manual-submit")
    assert ctx == {"path": "manual-submit", "type": "drop", "player_name": "Chief McD",
                   "guid": "abc", "used_api": True,
                   "npc_name": "NPC Reward pool (Tempoross)"}


def test_submission_context_truncates_long_values_and_tolerates_non_dicts():
    ctx = ca.submission_context({"item_name": "x" * 500}, "p")
    assert len(ctx["item_name"]) == 120
    assert ca.submission_context(None, "p") == {"path": "p"}


def test_origin_is_scoped_to_its_block_and_annotatable():
    assert ca.current_origin() is None
    ca.annotate_origin({"ignored": 1})  # no block: no-op
    with ca.catalog_origin({"path": "outer"}):
        ca.annotate_origin({"guid": "g1"})
        with ca.catalog_origin({"path": "inner"}):
            assert ca.current_origin() == {"path": "inner"}
        assert ca.current_origin() == {"path": "outer", "guid": "g1"}
    assert ca.current_origin() is None


def test_systemd_unit_parsed_from_cgroup():
    assert ca._systemd_unit("0::/system.slice/droptracker-api.service\n") == "droptracker-api.service"
    assert ca._systemd_unit("0::/user.slice/user-1000.slice/session-1.scope\n") is None


def test_caller_frames_skip_plumbing():
    stack = traceback.StackSummary.from_list([
        ("/store/droptracker/disc/data/submissions/common.py", 1105, "ensure_npc", None),
        ("/venv/lib/python3.11/site-packages/sqlalchemy/orm/unitofwork.py", 1, "flush", None),
        ("/store/droptracker/disc/utils/catalog_audit.py", 1, "after_insert", None),
    ])
    frames = ca.caller_frames(stack)
    assert len(frames) == 1
    assert frames[0].endswith("data/submissions/common.py:1105 ensure_npc")


def test_build_entry_shape():
    with ca.catalog_origin({"path": "dispatch:drop", "player_name": "p"}):
        entry = ca.build_entry("npc", 13952, "NPC Reward pool (Tempoross)")
    assert entry["action"] == "catalog.npc.create"
    assert entry["target"] == "npc_list.13952"
    after = json.loads(entry["after"])
    assert after["name"] == "NPC Reward pool (Tempoross)"
    assert after["origin"] == {"path": "dispatch:drop", "player_name": "p"}
    assert after["process"]
    assert isinstance(after["caller"], list)


# ── The hooks, against a real (SQLite) database ──────────────────────────────

@pytest.fixture
def catalog_db():
    Base = declarative_base()

    class Item(Base):
        __tablename__ = "items"
        item_id = Column(Integer, primary_key=True)
        item_name = Column(String(125))

    class Npc(Base):
        __tablename__ = "npc_list"
        npc_id = Column(Integer, primary_key=True)
        npc_name = Column(String(60), nullable=False)

    class Audit(Base):
        __tablename__ = "audit_log"
        id = Column(Integer, primary_key=True, autoincrement=True)
        actor_user_id = Column(Integer)
        action = Column(String(64), nullable=False)
        target = Column(String(128))
        after = Column(Text)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ca.register(Item, Npc, Audit.__table__)
    ca.register(Item, Npc, Audit.__table__)  # idempotent: one row per insert
    return sessionmaker(bind=engine), Item, Npc, Audit


def test_new_rows_write_one_audit_entry_each_in_the_same_transaction(catalog_db):
    make, Item, Npc, Audit = catalog_db
    s = make()
    with ca.catalog_origin({"path": "dispatch:drop", "guid": "g-1"}):
        s.add(Npc(npc_id=13952, npc_name="NPC Reward pool (Tempoross)"))
        s.add(Item(item_id=28798, item_name="Scurrius' spine"))
        s.commit()
    rows = s.execute(select(Audit.action, Audit.target, Audit.after)).all()
    assert sorted((a, t) for a, t, _ in rows) == [
        ("catalog.item.create", "items.28798"),
        ("catalog.npc.create", "npc_list.13952"),
    ]
    for _, _, after in rows:
        assert json.loads(after)["origin"] == {"path": "dispatch:drop", "guid": "g-1"}


def test_rolled_back_insert_leaves_no_audit_row(catalog_db):
    make, Item, _Npc, Audit = catalog_db
    s = make()
    s.add(Item(item_id=1, item_name="x"))
    s.flush()
    s.rollback()
    assert s.execute(select(Audit)).all() == []


def test_audit_failure_never_costs_the_catalogue_row(catalog_db, monkeypatch):
    make, Item, _Npc, _Audit = catalog_db

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(ca, "build_entry", boom)
    s = make()
    s.add(Item(item_id=2, item_name="y"))
    s.commit()
    assert s.get(Item, 2).item_name == "y"
