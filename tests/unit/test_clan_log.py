"""Clan Log: claim folding, period windows, shared slots, and the catalog.

The things that decide whether a board tells the truth:

* **Folding.** A month's cell names the *first* member to get the item, counts
  every occurrence, and lets a drop outrank a supplement at the same instant.
  Get this wrong and the board credits the wrong person, or double-counts a
  rebuild against a tail.
* **Period windows.** Month / year / all-time all read the same monthly ledger,
  so `period_contains` is the only thing keeping "what did we get in August"
  apart from "what have we ever got".
* **Shared slots.** An item on several sections (godsword shards, 3rd age on
  three clue tiers) is credited by the dropping NPC, or one drop fills them all.
* **Slot ids.** A slot is keyed by the id drops carry. A first-match name lookup
  put Vorkath's head on a 16-drop twin of the 6,995-drop id.
* **The catalog spec** against the real cache structure: the regressions from
  suggestion #207 (Tiny tempor on Tormented Demon, missing Moons armour).

Loaded from file paths so the conftest ``services`` stub doesn't shadow them.
"""

import importlib.util
import os
import sys
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str, *parts: str):
    path = os.path.join(_ROOT, *parts)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_with_stubs(name: str, parts: tuple, stubs: dict):
    """Load a module that imports from ``db.models.*``, then put sys.modules back.

    The conftest stubs ``db.models`` as a plain MagicMock, so it is not a
    package and ``db.models.clan_log`` cannot be imported from it. Injecting
    lightweight stand-ins is enough for the pure functions under test — but they
    MUST be removed again: leaving a fake ``db.models.recap`` behind breaks
    every later test that imports the real one, which is the same sys.modules
    poisoning that already cost this suite its green baseline once.
    """
    import types

    saved = {key: sys.modules.get(key) for key in stubs}
    try:
        for key, attrs in stubs.items():
            module = types.ModuleType(key)
            for attr, value in attrs.items():
                setattr(module, attr, value)
            sys.modules[key] = module
        return _load(name, *parts)
    finally:
        for key, original in saved.items():
            if original is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = original


clan_log = _load_with_stubs(
    "_clan_log_under_test",
    ("services", "clan_log.py"),
    {
        "db.models.clan_log": {
            "CLAN_LOG_SCHEMA_VERSION": 1,
            "PERIOD_ALL": "all",
            "SCOPE_CLAN_LOG": "clan_log",
            "SOURCE_DROP": "drop",
            "SOURCE_CLOG": "clog",
            "SOURCE_PET": "pet",
        },
        "db.models.recap": {"RecapSnapshot": object},
    },
)
catalog_source = _load(
    "_clan_log_catalog_under_test", "scripts", "clan_log", "catalog_source.py"
)
catalog_spec = _load(
    "_clan_log_spec_under_test", "scripts", "clan_log", "catalog_spec.py"
)


def _claim(item_id, player_id, when, source=None, ref_id=None, proof=None, npc_key=None):
    return {
        "item_id": item_id,
        "player_id": player_id,
        "at": when,
        "source": source or clan_log.SOURCE_DROP,
        "ref_id": ref_id,
        "proof": proof,
        "npc_key": npc_key,
    }


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "period,ok",
    [
        ("all", True), ("2026", True), ("2026-08", True), ("2026-12", True),
        ("2026-13", False), ("2026-00", False), ("26-08", False),
        ("", False), ("august", False), ("2026-8", False),
    ],
)
def test_is_valid_period(period, ok):
    assert clan_log.is_valid_period(period) is ok


def test_period_contains_windows():
    # All-time takes everything; a year takes its months; a month takes itself.
    assert clan_log.period_contains("all", "2024-01")
    assert clan_log.period_contains("2026", "2026-08")
    assert not clan_log.period_contains("2026", "2025-12")
    assert clan_log.period_contains("2026-08", "2026-08")
    assert not clan_log.period_contains("2026-08", "2026-07")


def test_current_periods_are_all_year_month():
    periods = clan_log.current_periods(datetime(2026, 8, 13))
    assert periods == ["all", "2026", "2026-08"]


# --------------------------------------------------------------------------- #
# Folding claims into the monthly ledger
# --------------------------------------------------------------------------- #
def test_fold_splits_by_month_and_keeps_first_per_month():
    canonical = {100: 100}
    folded = clan_log.fold_claims(
        [
            _claim(100, 7, datetime(2026, 8, 20)),
            _claim(100, 3, datetime(2026, 8, 2)),   # earliest in August
            _claim(100, 9, datetime(2026, 7, 30)),  # different month = own row
        ],
        canonical,
    )
    assert set(folded) == {(100, "2026-08"), (100, "2026-07")}
    assert folded[(100, "2026-08")]["player_id"] == 3
    assert folded[(100, "2026-08")]["obtained_count"] == 2
    assert folded[(100, "2026-08")]["player_count"] == 2
    assert folded[(100, "2026-07")]["player_id"] == 9


def test_fold_prefers_a_drop_over_a_supplement_at_the_same_instant():
    """A clog unlock and its drop can land on the same timestamp. The drop is
    the evidence, so it must own the row (and carry its drop_id)."""
    when = datetime(2026, 8, 5, 12, 0, 0)
    folded = clan_log.fold_claims(
        [
            _claim(100, 5, when, source=clan_log.SOURCE_CLOG),
            _claim(100, 6, when, source=clan_log.SOURCE_DROP, ref_id=4242),
        ],
        {100: 100},
    )
    row = folded[(100, "2026-08")]
    assert row["source"] == clan_log.SOURCE_DROP
    assert row["player_id"] == 6
    assert row["drop_id"] == 4242


def test_fold_maps_variants_onto_the_canonical_slot():
    """An uncharged Scythe and a charged one are one board cell, not two."""
    folded = clan_log.fold_claims(
        [
            _claim(222, 1, datetime(2026, 8, 1)),   # variant id
            _claim(111, 2, datetime(2026, 8, 4)),   # canonical id
        ],
        {111: 111, 222: 111},
    )
    assert set(folded) == {(111, "2026-08")}
    assert folded[(111, "2026-08")]["obtained_count"] == 2
    assert folded[(111, "2026-08")]["player_id"] == 1


def test_fold_ignores_items_outside_the_catalog():
    assert clan_log.fold_claims([_claim(999, 1, datetime(2026, 8, 1))], {100: 100}) == {}


def test_fold_only_drops_carry_a_drop_id():
    folded = clan_log.fold_claims(
        [_claim(100, 1, datetime(2026, 8, 1), source=clan_log.SOURCE_PET, ref_id=55)],
        {100: 100},
    )
    assert folded[(100, "2026-08")]["drop_id"] is None


# --------------------------------------------------------------------------- #
# Proof URLs
# --------------------------------------------------------------------------- #
def test_clean_proof_maps_on_disk_paths_and_drops_junk():
    # `drops.image_url` is not reliably a URL — some rows hold the disk path.
    assert clan_log._clean_proof(
        "/store/droptracker/disc/static/assets/img/user-upload/x.png"
    ) == "https://www.droptracker.io/img/user-upload/x.png"
    assert clan_log._clean_proof("https://example.com/a.png") == "https://example.com/a.png"
    assert clan_log._clean_proof("some-local-file.png") is None
    assert clan_log._clean_proof(None) is None


# --------------------------------------------------------------------------- #
# Shared slots
# --------------------------------------------------------------------------- #
SHARD = 11818


def _sections():
    return [
        {"id": 1, "npc_keys": ["general-graardor", "sergeant-strongstack"], "items": [
            {"item_id": SHARD, "source": "drop", "slot_id": SHARD},
            {"item_id": 11832, "source": "drop", "slot_id": 11832},
        ]},
        {"id": 2, "npc_keys": ["kree-arra"], "items": [
            {"item_id": SHARD, "source": "drop", "slot_id": SHARD},
        ]},
    ]


def test_scope_gives_each_section_its_own_slot_for_a_shared_item():
    sections = _sections()
    scoped = clan_log.scope_shared_items(sections)
    graardor, kree = sections[0]["items"][0], sections[1]["items"][0]
    assert graardor["slot_id"] != kree["slot_id"]
    assert graardor["slot_id"] < 0 and kree["slot_id"] < 0
    assert scoped[SHARD] == {
        "general-graardor": graardor["slot_id"],
        "sergeant-strongstack": graardor["slot_id"],
        "kree-arra": kree["slot_id"],
    }
    # An item on one section only keeps its own id.
    assert sections[0]["items"][1]["slot_id"] == 11832
    assert 11832 not in scoped


def test_fold_credits_a_shared_item_only_to_the_dropping_bosss_section():
    sections = _sections()
    scoped = clan_log.scope_shared_items(sections)
    folded = clan_log.fold_claims(
        [_claim(SHARD, 1, datetime(2026, 8, 1), npc_key="sergeant-strongstack")],
        {SHARD: SHARD}, scoped,
    )
    assert set(folded) == {(sections[0]["items"][0]["slot_id"], "2026-08")}


def test_fold_drops_a_shared_item_no_section_claims():
    """A visage from a frost dragon fills neither Vorkath nor KBD, and a
    collection-log unlock carries no NPC at all."""
    scoped = clan_log.scope_shared_items(_sections())
    claims = [
        _claim(SHARD, 1, datetime(2026, 8, 1), npc_key="frost-dragon"),
        _claim(SHARD, 1, datetime(2026, 8, 1), source=clan_log.SOURCE_CLOG),
    ]
    assert clan_log.fold_claims(claims, {SHARD: SHARD}, scoped) == {}


def test_pets_are_never_scoped():
    sections = [
        {"id": 1, "npc_keys": ["chaos-elemental"],
         "items": [{"item_id": 11995, "source": "pet", "slot_id": 11995}]},
        {"id": 2, "npc_keys": ["chaos-fanatic"],
         "items": [{"item_id": 11995, "source": "pet", "slot_id": 11995}]},
    ]
    assert clan_log.scope_shared_items(sections) == {}


# --------------------------------------------------------------------------- #
# Slot ids
# --------------------------------------------------------------------------- #
def test_pick_slot_ids_keys_the_slot_by_the_id_drops_carry():
    primary, variants = catalog_source.pick_slot_ids(
        21907, {2425, 21907}, {2425: 16, 21907: 6995})
    assert primary == 21907
    assert variants == [2425]


def test_pick_slot_ids_keeps_the_drawn_id_as_a_variant():
    """Unsired is stored as 13273 and drawn as 25624; a clog sync reports 25624."""
    primary, variants = catalog_source.pick_slot_ids(25624, {13273}, {13273: 3419})
    assert primary == 13273
    assert variants == [25624]


def test_pick_slot_ids_without_evidence_falls_back_to_the_drawn_id():
    primary, variants = catalog_source.pick_slot_ids(20014, {20014, 20015}, {})
    assert primary == 20014
    assert variants == []


def test_slug_and_variant_helpers():
    assert catalog_source.slugify("Vet'ion / Calvar'ion") == "vet_ion_calvar_ion"
    assert catalog_source.base_item_name("Scythe of vitur (uncharged)") == "Scythe of vitur"
    assert catalog_source.base_item_name("Tome of Fire (empty)") == "Tome of Fire"
    assert catalog_source.base_item_name("Twisted bow") == "Twisted bow"


# --------------------------------------------------------------------------- #
# Catalog spec, against the tracked cache structure
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def spec_sections():
    return catalog_spec.build_sections(catalog_spec.load_structure())


def _section(sections, label):
    return next(s for s in sections if s["label"] == label)


def _names(section):
    return {i["item_name"] for i in section["items"]}


def test_spec_slugs_are_unique(spec_sections):
    slugs = [s["slug"] for s in spec_sections]
    assert len(slugs) == len(set(slugs))


def test_spec_fixes_the_suggestion_207_gaps(spec_sections):
    assert "Tiny tempor" not in _names(_section(spec_sections, "Tormented Demons"))
    assert "Tiny tempor" in _names(_section(spec_sections, "Tempoross"))
    assert {"Eclipse moon helm", "Eclipse moon chestplate", "Eclipse moon tassets",
            "Eclipse atlatl"} == _names(_section(spec_sections, "Eclipse Moon"))
    for boss in ("Corporeal Beast", "Yama", "Doom of Mokhaiotl", "Maggot King"):
        assert _section(spec_sections, boss)["category"] == "bosses"
    assert _section(spec_sections, "Skotizo")["category"] == "slayer"
    assert _section(spec_sections, "Sarachnis")["category"] == "bosses"
    assert _section(spec_sections, "Shellbane Gryphon")["category"] == "bosses"
    assert {"Ring of nature", "Lava dragon mask"} <= _names(_section(spec_sections, "Elite (rare)"))
    assert {"Ring of coins", "Bucket helm (g)"} <= _names(_section(spec_sections, "Master (rare)"))
    assert "Mystic hat (dusk)" in _names(_section(spec_sections, "Slayer (non-boss)"))


def test_spec_never_lists_an_excluded_slot(spec_sections):
    for section in spec_sections:
        assert not (_names(section) & catalog_spec.EXCLUDE), section["label"]


def test_spec_marks_pets_and_clog_only_slots_non_attributable(spec_sections):
    tempoross = {i["item_name"]: i for i in _section(spec_sections, "Tempoross")["items"]}
    assert tempoross["Tiny tempor"]["source_hint"] == "pet"
    assert not tempoross["Tiny tempor"]["attributable"]
    misc = {i["item_name"]: i for i in _section(spec_sections, "Miscellaneous")["items"]}
    assert misc["Evil chicken head"]["source_hint"] == "clog"
    assert not misc["Evil chicken head"]["attributable"]
    assert misc["Dragon warhammer"]["attributable"]


def test_every_shared_item_can_be_credited(spec_sections):
    assert catalog_spec.shared_items_without_npc_keys(spec_sections) == []


def test_godsword_shards_credit_the_generals_bodyguards(spec_sections):
    assert "sergeant-strongstack" in _section(spec_sections, "General Graardor")["npc_keys"]
    assert "clue-scroll-master" in _section(spec_sections, "Master (rare)")["npc_keys"]


# --------------------------------------------------------------------------- #
# Drop reads: forced index on full reads, prefetch for many-group rebuilds
# --------------------------------------------------------------------------- #
class _FakeSession:
    """Records each statement; answers drop reads from canned rows."""

    def __init__(self, drop_rows=(), max_drop_id=0):
        self.sql = []
        self.drop_rows = list(drop_rows)
        self.max_drop_id = max_drop_id

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append((sql, dict(params or {})))
        rows = []
        if "MAX(drop_id)" in sql:
            rows = [(self.max_drop_id,)]
        elif "FROM drops" in sql:
            players = (params or {}).get("players")
            cursor = (params or {}).get("cursor")
            rows = [r for r in self.drop_rows
                    if (players is None or r[2] in players)
                    and (cursor is None or r[0] > cursor)]

        class _Result:
            def fetchall(self_inner):
                return rows

            def first(self_inner):
                return rows[0] if rows else None

        return _Result()


def _drop_row(drop_id, item_id, player_id, when=datetime(2026, 8, 1), npc_id=1):
    return (drop_id, item_id, player_id, when, None, npc_id)


def test_a_full_drop_read_forces_the_item_index_and_a_tail_does_not():
    session = _FakeSession()
    clan_log._claims_from_drops(session, [100], [1, 2])
    clan_log._claims_from_drops(session, [100], [1, 2], since_drop_id=50)
    full, tail = session.sql[0][0], session.sql[1][0]
    assert "FORCE INDEX (ix_drops_item_id)" in full
    assert "FORCE INDEX" not in tail
    assert "drop_id > :cursor" in tail


def test_an_all_player_read_has_no_roster_filter():
    session = _FakeSession([_drop_row(1, 100, 7), _drop_row(2, 100, 8)])
    claims = clan_log._claims_from_drops(session, [100], None)
    assert "player_id IN" not in session.sql[0][0]
    assert {c["player_id"] for c in claims} == {7, 8}
    # An empty roster still reads nothing.
    assert clan_log._claims_from_drops(_FakeSession(), [100], []) == []


def test_prefetch_drops_rows_above_the_ceiling_read_before_the_scan():
    session = _FakeSession([_drop_row(10, 100, 7), _drop_row(20, 100, 7)], max_drop_id=15)
    prefetched = clan_log.prefetch_drop_claims(session, {"canonical": {100: 100}})
    assert prefetched["max_drop_id"] == 15
    assert [c["ref_id"] for c in prefetched["claims"]] == [10]


def test_a_prefetched_rebuild_takes_its_roster_share_and_tails_without_double_counting(
        monkeypatch):
    catalog = {"sections": [], "canonical": {100: 100}, "scoped": {}}
    prefetched = {"claims": [
        {"ref_id": 10, "item_id": 100, "player_id": 7, "at": datetime(2026, 8, 1),
         "proof": None, "source": "drop", "npc_id": 1},
        {"ref_id": 11, "item_id": 100, "player_id": 99, "at": datetime(2026, 8, 1),
         "proof": None, "source": "drop", "npc_id": 1},
    ], "max_drop_id": 15}
    # Drop 10 is below the ceiling (already prefetched); 20 landed after it.
    session = _FakeSession([_drop_row(10, 100, 7), _drop_row(20, 100, 7)])
    captured = {}
    monkeypatch.setattr(clan_log, "_claims_from_collection", lambda *a, **k: [])
    monkeypatch.setattr(clan_log, "_claims_from_pets", lambda *a, **k: [])
    monkeypatch.setattr(clan_log, "upsert_ledger",
                        lambda s, gid, folded: captured.update(folded) or {})
    clan_log.rebuild_group(session, 5, catalog=catalog, player_ids=[7],
                           prefetched=prefetched)
    row = captured[(100, "2026-08")]
    assert row["obtained_count"] == 2      # drop 10 once + drop 20, never 10 twice
    assert row["player_count"] == 1        # player 99 is not on this roster
    # The shared prefetch list is never written into.
    assert "npc_key" not in prefetched["claims"][0]
