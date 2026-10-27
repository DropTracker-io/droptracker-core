"""Which duplicate pets a group announces.

The notification-side twin of ``data/submissions/pet_points.py``: a group
announces every new pet (``notify_pets``), and decides separately whether a
duplicate is announced too: every duplicate (the default, and the behaviour
before this setting existed), none, or only the pets it picks.

Keys (``group_configurations``, editable from the config registry, with
``seasonal_`` mirrors like ``notify_pets``):

* ``notify_pet_duplicates``: ``all`` | ``none`` | ``selected``. Absent = ``all``.
* ``notify_pet_duplicate_pets``: JSON array of canonical pet names, read only in
  ``selected`` mode. Stored in ``long_value`` (a full list runs past the 255
  characters ``config_value`` holds), mirrored into ``config_value`` when short.

A duplicate is announced through the ``pet_dupe`` design when the group has one
(embed or Components layout), otherwise through its pet design
(``services/notification_service.py``).
"""

from __future__ import annotations

import json

DUPLICATES_MODE_KEY = "notify_pet_duplicates"
DUPLICATE_PETS_KEY = "notify_pet_duplicate_pets"

DUPLICATE_MODES = ("all", "none", "selected")
DEFAULT_DUPLICATE_MODE = "all"

# The embed / component-layout type used for duplicates. group_embeds.embed_type
# is VARCHAR(10), which is why it isn't spelled "pet_duplicate".
DUPE_NOTIFICATION_TYPE = "pet_dupe"

_TRUE_STRINGS = ("true", "1", "yes", "on")


def _norm(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def flag_is_true(value) -> bool:
    """The plugin's ``duplicate`` flag. It reaches the processor as a real bool
    from the API and as the STRING "true"/"false" from webhook intake, where a
    plain truthiness test reads "false" as a duplicate."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return _norm(value) in _TRUE_STRINGS


def is_duplicate(is_new_pet: bool, duplicate_flag=None) -> bool:
    """Whether a pet drop is a duplicate. The game's own word wins when the
    plugin sent it ("would have been followed" is a duplicate even for a pet
    DropTracker never recorded, and a first follow is new even if an adventure
    log sync already recorded the pet). Without the flag, a pet DropTracker
    already has for this player is a duplicate."""
    if duplicate_flag is None or duplicate_flag == "":
        return not is_new_pet
    return flag_is_true(duplicate_flag)


def parse_pet_list(raw) -> tuple:
    """Pet names from a stored value: a JSON array, or (hand-typed, legacy) a
    comma-separated list. Anything unreadable is an empty list."""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        names = raw
    else:
        text = str(raw).strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                names = json.loads(text)
            except (TypeError, ValueError):
                return ()
            if not isinstance(names, list):
                return ()
        else:
            names = text.split(",")
    return tuple(str(n).strip() for n in names if isinstance(n, str) and n.strip())


def duplicate_announced(mode, pet_names, pet_name: str) -> bool:
    """Whether a duplicate of ``pet_name`` is announced under these settings.
    An unknown mode reads as the default, which announces."""
    mode = _norm(mode) or DEFAULT_DUPLICATE_MODE
    if mode == "none":
        return False
    if mode == "selected":
        wanted = {_norm(n) for n in pet_names or ()}
        return _norm(pet_name) in wanted
    return True


def _stored(row):
    if row is None:
        return None
    long_value = getattr(row, "long_value", None)
    if long_value:
        return long_value
    return getattr(row, "config_value", None)


def load_settings(session, group_id: int, prefix: str = "") -> tuple:
    """``(mode, pet_names)`` for a group; ``prefix`` is ``"seasonal_"`` or
    ``"hardcore_"`` for those worlds, like every other notify key. A group that
    shares its main settings with Hardcore Worlds reads the main keys."""
    from db.models import GroupConfiguration
    from utils import group_config as gc

    prefix = gc.resolve_prefix(session, group_id, prefix)

    mode_key = f"{prefix}{DUPLICATES_MODE_KEY}"
    list_key = f"{prefix}{DUPLICATE_PETS_KEY}"
    rows = (
        session.query(GroupConfiguration)
        .filter(
            GroupConfiguration.group_id == int(group_id),
            GroupConfiguration.config_key.in_((mode_key, list_key)),
        )
        .all()
    )
    by_key = {r.config_key: r for r in rows}
    mode = _norm(_stored(by_key.get(mode_key))) or DEFAULT_DUPLICATE_MODE
    if mode not in DUPLICATE_MODES:
        mode = DEFAULT_DUPLICATE_MODE
    return mode, parse_pet_list(_stored(by_key.get(list_key)))


def group_announces_duplicate(session, group_id: int, pet_name: str, prefix: str = "") -> bool:
    """Whether this group announces a duplicate of ``pet_name``. A failed read
    announces, which is what every group did before the setting existed."""
    try:
        mode, names = load_settings(session, group_id, prefix)
    except Exception as e:
        print(f"[PetNotify] couldn't read duplicate settings for group {group_id}: {e}")
        return True
    return duplicate_announced(mode, names, pet_name)
