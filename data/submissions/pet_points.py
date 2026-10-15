"""Clan points for pets: the once-per-pet "new pet" award, duplicates, and
pets a member owned before DropTracker saw them.

The rule this module owns: **a member earns a pet's new-pet points once per
group.** Every later copy of that pet is a duplicate, and a duplicate pays only
when the group says so (``points_pet_duplicates``: never by default, every pet,
or a chosen list), at the new-pet rate or a flat amount of its own.

"Already paid" is read from the ledger, not from ``player_pets``. That table
answers "has DropTracker seen this pet", which is the wrong question for points:
a pet can be recorded (adventure log, or before the player joined this group,
or before the group had points) without ever having paid here. Reading the
ledger means the first copy a group sees pays as new, which is the behaviour
with ``points_pet_prior_owned`` off.

With ``points_pet_prior_owned`` on, :func:`sweep_prior_owned_pets` pays the
new-pet award for pets members already own according to their stored
collection log (``player_clog_items``) and DropTracker's pet records. It runs
when an admin turns the option on (with a preview first) and hourly after that
(``scripts/award_prior_owned_pets.py``), so a member who joins or syncs later
is picked up. Because those rows count as the new-pet award, the next in-game
copy is a duplicate.

Ledger shapes (``player_points``):

* new pet, live: ``reason='pet'``, ``entry_type`` NULL, ``entry_id`` =
  ``player_pets.id``. Unchanged from before this module, so every existing row
  already counts as "paid". Duplicates that paid before 2026-10-15 have this
  shape too, which is harmless: their pet's first award exists anyway.
* duplicate: ``reason='pet (duplicate)'``, ``entry_type`` 20, ``entry_id`` =
  ``player_pets.id``.
* previously owned: ``reason='pet (previously owned)'``, ``entry_type`` 21,
  ``entry_id`` = the pet's item id (a clog-only pet has no ``player_pets`` row,
  and creating one would change event-task and notification behaviour).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import and_, or_

from utils.osrs_pets import PET_DISPLAY_BY_NORM, canonical_pet_name

DUPLICATES_MODE_KEY = "points_pet_duplicates"
DUPLICATE_PETS_KEY = "points_pet_duplicate_pets"
DUPLICATE_AWARD_KEY = "points_pet_duplicate_award"
PRIOR_OWNED_KEY = "points_pet_prior_owned"

DUPLICATE_MODES = ("none", "all", "selected")
DEFAULT_DUPLICATE_MODE = "none"

DUPLICATE_ENTRY_TYPE = 20
PRIOR_OWNED_ENTRY_TYPE = 21
DUPLICATE_REASON = "pet (duplicate)"
PRIOR_OWNED_REASON = "pet (previously owned)"

MAX_DUPLICATE_AWARD = 1_000_000


def _norm(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


@dataclass(frozen=True)
class PetPointSettings:
    duplicates: str = DEFAULT_DUPLICATE_MODE
    #: Canonical display names; only read in "selected" mode.
    duplicate_pets: tuple = ()
    #: None = pay a duplicate what a new copy of that pet pays.
    duplicate_award: int | None = None
    prior_owned: bool = False

    def duplicate_pays(self, pet_name: str) -> bool:
        if self.duplicates == "all":
            return True
        if self.duplicates == "selected":
            wanted = {_norm(n) for n in self.duplicate_pets}
            return _norm(pet_name) in wanted
        return False

    def as_payload(self) -> dict:
        return {
            "pet_duplicates": self.duplicates,
            "pet_duplicate_pets": list(self.duplicate_pets),
            "pet_duplicate_award": self.duplicate_award,
            "pet_prior_owned": self.prior_owned,
        }


def _config_rows(session, group_id: int) -> dict:
    from db.models import GroupConfiguration

    rows = (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == int(group_id),
            GroupConfiguration.config_key.in_(
                (DUPLICATES_MODE_KEY, DUPLICATE_PETS_KEY, DUPLICATE_AWARD_KEY, PRIOR_OWNED_KEY)
            ),
        )
        .all()
    )
    return {r.config_key: r for r in rows}


def parse_settings(rows: dict) -> PetPointSettings:
    """Settings from ``{config_key: row}``. Anything unreadable falls back to
    the default, which is the safe direction: no duplicate pays."""
    mode_row = rows.get(DUPLICATES_MODE_KEY)
    mode = _norm(getattr(mode_row, "config_value", None)) or DEFAULT_DUPLICATE_MODE
    if mode not in DUPLICATE_MODES:
        mode = DEFAULT_DUPLICATE_MODE

    pets: tuple = ()
    list_row = rows.get(DUPLICATE_PETS_KEY)
    raw = getattr(list_row, "long_value", None) if list_row is not None else None
    if raw:
        try:
            names = json.loads(raw)
            if isinstance(names, list):
                pets = tuple(str(n) for n in names if isinstance(n, str) and n.strip())
        except Exception:
            pets = ()

    award = None
    award_row = rows.get(DUPLICATE_AWARD_KEY)
    raw_award = str(getattr(award_row, "config_value", "") or "").strip()
    if raw_award:
        try:
            award = max(0, min(MAX_DUPLICATE_AWARD, int(raw_award)))
        except Exception:
            award = None

    prior_row = rows.get(PRIOR_OWNED_KEY)
    prior = str(getattr(prior_row, "config_value", "") or "").strip() == "1"
    return PetPointSettings(duplicates=mode, duplicate_pets=pets,
                            duplicate_award=award, prior_owned=prior)


def load_settings(session, group_id: int) -> PetPointSettings:
    return parse_settings(_config_rows(session, group_id))


def normalize_duplicate_pets(names) -> tuple[list, list]:
    """``(canonical, unknown)`` for an admin-submitted pet list: known pets
    in their display spelling, deduped and sorted; the rest reported back."""
    known, unknown = set(), []
    for raw in names or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        canon = canonical_pet_name(raw)
        if canon is None:
            unknown.append(raw.strip())
        else:
            known.add(canon)
    return sorted(known), unknown


def write_settings(session, group_id: int, *, duplicates=None, duplicate_pets=None,
                   duplicate_award=..., prior_owned=None) -> None:
    """Store whichever settings are given (``duplicate_award=None`` clears the
    custom amount: "same as a new pet"). Values must already be validated."""
    from db.models import GroupConfiguration

    rows = _config_rows(session, group_id)

    def put(key, value, long_value=None):
        row = rows.get(key)
        if row is None:
            row = GroupConfiguration(group_id=int(group_id), config_key=key, config_value=value)
            session.add(row)
            rows[key] = row
        row.config_value = value
        if key == DUPLICATE_PETS_KEY:
            row.long_value = long_value

    if duplicates is not None:
        put(DUPLICATES_MODE_KEY, duplicates)
    if duplicate_pets is not None:
        put(DUPLICATE_PETS_KEY, "", json.dumps(list(duplicate_pets)))
    if duplicate_award is not ...:
        put(DUPLICATE_AWARD_KEY, "" if duplicate_award is None else str(int(duplicate_award)))
    if prior_owned is not None:
        put(PRIOR_OWNED_KEY, "1" if prior_owned else "0")


# --------------------------------------------------------------------------- #
# "Has this member already been paid for this pet here?"
# --------------------------------------------------------------------------- #
def _pet_item_ids(session, pet_name: str, item_id=None) -> set:
    """Every item id carrying this pet's name (a pet can have several rows:
    metamorph variants, re-added items), plus the id the submission used."""
    from db.models import ItemList

    ids = set()
    if item_id:
        try:
            ids.add(int(item_id))
        except Exception:
            pass
    names = {n for n in (pet_name, canonical_pet_name(pet_name)) if n}
    if names:
        for (iid,) in session.query(ItemList.item_id).filter(ItemList.item_name.in_(names)).all():
            ids.add(int(iid))
    return ids


def new_pet_award_exists(session, group_id: int, player_id: int, pet_name: str,
                         item_id=None) -> bool:
    from db.models import PlayerPet, PlayerPoints

    item_ids = _pet_item_ids(session, pet_name, item_id)
    if not item_ids:
        return False
    pet_row_ids = [
        int(pid) for (pid,) in session.query(PlayerPet.id)
        .filter(PlayerPet.player_id == int(player_id), PlayerPet.item_id.in_(item_ids))
        .all()
    ]
    paid_live = and_(PlayerPoints.reason == "pet", PlayerPoints.entry_type.is_(None),
                     PlayerPoints.entry_id.in_(pet_row_ids)) if pet_row_ids else None
    paid_prior = and_(PlayerPoints.entry_type == PRIOR_OWNED_ENTRY_TYPE,
                      PlayerPoints.entry_id.in_(item_ids))
    cond = or_(paid_live, paid_prior) if paid_live is not None else paid_prior
    hit = (
        session.query(PlayerPoints.id)
        .filter(PlayerPoints.group_id == int(group_id),
                PlayerPoints.player_id == int(player_id), cond)
        .first()
    )
    return hit is not None


@dataclass
class PetAwardPlan:
    """What a pet submission should do to one group's points."""
    award: bool
    duplicate: bool
    duplicate_award: int | None = None


def plan_live_pet(session, group_id: int, player_id: int, pet_name: str,
                  item_id=None, settings: PetPointSettings | None = None) -> PetAwardPlan:
    if not new_pet_award_exists(session, group_id, player_id, pet_name, item_id):
        return PetAwardPlan(award=True, duplicate=False)
    settings = settings or load_settings(session, group_id)
    if not settings.duplicate_pays(pet_name):
        return PetAwardPlan(award=False, duplicate=True)
    return PetAwardPlan(award=True, duplicate=True, duplicate_award=settings.duplicate_award)


# --------------------------------------------------------------------------- #
# Previously-owned sweep
# --------------------------------------------------------------------------- #
@dataclass
class SweepResult:
    members: int = 0
    awards: int = 0
    points: int = 0
    #: [{"pet": name, "item_id": id, "members": n, "points_each": p}]
    by_pet: list = field(default_factory=list)

    def as_payload(self) -> dict:
        return {"members": self.members, "awards": self.awards,
                "points": self.points, "by_pet": self.by_pet}


def _member_player_ids(session, group_id: int) -> set:
    from sqlalchemy import text

    rows = session.execute(
        text("SELECT DISTINCT player_id FROM user_group_association "
             "WHERE group_id = :g AND player_id IS NOT NULL"),
        {"g": int(group_id)},
    ).fetchall()
    return {int(r[0]) for r in rows}


def _owned_pets(session, player_ids: set) -> dict:
    """``{player_id: {canonical_name: item_id}}`` from the stored collection
    log and DropTracker's own pet records."""
    from db.models import ItemList, PlayerPet
    from db.models.player_state import PlayerCollectionLogItem

    owned: dict = {}
    if not player_ids:
        return owned
    names = list(PET_DISPLAY_BY_NORM.values())
    ids = list(player_ids)

    def add(pid, name, iid):
        canon = canonical_pet_name(name)
        if canon is None or not iid:
            return
        owned.setdefault(int(pid), {}).setdefault(canon, int(iid))

    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for pid, iid, name in (
            session.query(PlayerCollectionLogItem.player_id, PlayerCollectionLogItem.item_id,
                          ItemList.item_name)
            .join(ItemList, ItemList.item_id == PlayerCollectionLogItem.item_id)
            .filter(PlayerCollectionLogItem.player_id.in_(chunk),
                    PlayerCollectionLogItem.quantity > 0,
                    ItemList.item_name.in_(names))
            .all()
        ):
            add(pid, name, iid)
        for pid, iid, name in (
            session.query(PlayerPet.player_id, PlayerPet.item_id, PlayerPet.pet_name)
            .filter(PlayerPet.player_id.in_(chunk))
            .all()
        ):
            add(pid, name, iid)
    return owned


def _paid_pets(session, group_id: int, player_ids: set) -> set:
    """``{(player_id, canonical_name)}`` already holding a new-pet award here."""
    from db.models import ItemList, PlayerPet, PlayerPoints

    paid = set()
    if not player_ids:
        return paid
    ids = list(player_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for pid, name in (
            session.query(PlayerPoints.player_id, PlayerPet.pet_name)
            .join(PlayerPet, PlayerPet.id == PlayerPoints.entry_id)
            .filter(PlayerPoints.group_id == int(group_id),
                    PlayerPoints.player_id.in_(chunk),
                    PlayerPoints.reason == "pet",
                    PlayerPoints.entry_type.is_(None))
            .all()
        ):
            canon = canonical_pet_name(name)
            if canon:
                paid.add((int(pid), canon))
        for pid, name in (
            session.query(PlayerPoints.player_id, ItemList.item_name)
            .join(ItemList, ItemList.item_id == PlayerPoints.entry_id)
            .filter(PlayerPoints.group_id == int(group_id),
                    PlayerPoints.player_id.in_(chunk),
                    PlayerPoints.entry_type == PRIOR_OWNED_ENTRY_TYPE)
            .all()
        ):
            canon = canonical_pet_name(name)
            if canon:
                paid.add((int(pid), canon))
    return paid


async def sweep_prior_owned_pets(session, group_id: int, *, apply: bool,
                                 player_ids=None) -> SweepResult:
    """Pay the new-pet award for pets current members own but were never paid
    for in this group. ``apply=False`` is the preview: same numbers, no writes.

    The amount per pet comes from the normal award engine (rule, per-pet
    overrides, include/exclude lists, per-submission limits) with timed boosts
    skipped: a boost running today says nothing about a pet obtained years ago.
    Writes go through the caller's session; the caller commits.
    """
    from data.submissions.common import check_group_point_system_active
    from data.submissions.point_awards import award_player_points, check_and_award_points

    result = SweepResult()
    if not check_group_point_system_active(group_id, session):
        return result
    members = _member_player_ids(session, group_id)
    if player_ids is not None:
        members &= {int(p) for p in player_ids}
    owned = _owned_pets(session, members)
    paid = _paid_pets(session, group_id, set(owned))

    todo: dict = {}  # canonical name -> [(player_id, item_id)]
    for pid, pets in owned.items():
        for name, iid in pets.items():
            if (pid, name) not in paid:
                todo.setdefault(name, []).append((pid, iid))

    awarded_members = set()
    for name in sorted(todo):
        holders = todo[name]
        sample_pid, sample_iid = holders[0]
        quote = await check_and_award_points(
            "pet", group_id, sample_pid, 1, item_id=sample_iid,
            dry_run=True, skip_boosts=True, external_session=session,
        )
        each = int(quote.get("receiver_points_awarded") or 0)
        if each <= 0:
            continue
        result.by_pet.append({"pet": name, "item_id": int(sample_iid),
                              "members": len(holders), "points_each": each})
        for pid, iid in holders:
            if apply:
                written = await award_player_points(
                    PRIOR_OWNED_REASON, group_id, pid, each, entry_id=iid,
                    entry_type=PRIOR_OWNED_ENTRY_TYPE, external_session=session,
                )
                if not written:
                    continue
            awarded_members.add(pid)
            result.awards += 1
            result.points += each
    result.members = len(awarded_members)
    result.by_pet.sort(key=lambda r: (-r["members"] * r["points_each"], r["pet"]))
    return result
