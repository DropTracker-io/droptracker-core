"""Safe deaths — the group setting that decides whether they are announced.

Dying in Castle Wars, a Gauntlet run, a raid wipe or your own POH costs
nothing, and a clan feed full of them buries the deaths people actually want to
see. Dink solves this client-side with ``deathIgnoreSafe`` (default on, per
user); this is the group-side equivalent, so a clan gets one answer for all its
members instead of depending on what each of them ticked.

Like ``db.notification_blacklist``, this is **one rule read by two gates** —
``data/submissions/common.create_notification`` when a death is queued, and
``services/notification_service`` when it is sent — so a leader flipping the
setting also affects deaths already sitting in the queue, and the two gates
cannot drift apart.

**Default: safe deaths are NOT announced.** A group turns them back on with
``notify_deaths_safe``. This matches Dink's default and is a deliberate change
from the old behaviour of announcing every death.

Only group notifications are filtered. ``dm_death`` — the supporter's personal
submission DM — is out of scope for the same reason it escapes the blacklist:
it is the player's own record of their own death, not a clan's feed, and no
group's settings should reach into it.
"""
from __future__ import annotations

from utils import death_regions

#: Group config key. Truthy means "yes, still announce safe deaths".
SAFE_DEATH_CONFIG_KEY = "notify_deaths_safe"

#: Notification types this filter applies to. Deliberately not ``dm_death``.
FILTERABLE_TYPES = frozenset({"death"})

#: Seasonal submissions read their own prefixed config keys. Defined here
#: rather than imported so this module stays loadable by the web API without
#: dragging in the submission processors (the same reason
#: ``data/submissions/dispatch.py`` keeps its own copy).
SEASONAL_WORLD_TYPE = "seasonal"

_TRUE = frozenset({"true", "1", "yes", "y", "t"})
_FALSE = frozenset({"false", "0", "no", "n", "f"})


def parse_flag(value) -> bool | None:
    """A payload boolean, or ``None`` when the field is absent or unreadable.

    The plugin builds embed fields with ``String.valueOf``, so what arrives is
    the *string* ``"true"``/``"false"`` — and a value the plugin had nothing
    for becomes the literal ``"N/A"``. Distinguishing "absent" from "false"
    is the whole point: absent means fall back to the region, false means the
    client already told us this death was dangerous.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def is_safe_death(data) -> bool:
    """Whether this death cost the player nothing.

    Prefers the client's own answer: only the client can see account type
    (every death is dangerous for a hardcore ironman) and the live Pest Control
    overlay. Falls back to classifying the region alone when the flag is
    missing — a pre-6.0 client, or a manual submission.

    A death with neither signal is treated as **dangerous**, so it is announced.
    Guessing "safe" here would silently swallow real deaths.
    """
    if not isinstance(data, dict):
        return False
    flag = parse_flag(data.get("is_safe_death"))
    if flag is not None:
        return flag
    return death_regions.is_safe_region(data.get("region_id"))


def announces_safe_deaths(db_session, group_id, world_type=None) -> bool:
    """Whether this group asked to keep seeing safe deaths.

    A missing row means the default, which is *no*. A config read that fails
    is treated the same way rather than failing open: the alternative is that a
    transient database fault floods every group's feed with exactly the deaths
    they configured away.
    """
    from utils import group_config as gc

    prefix = "seasonal_" if world_type == SEASONAL_WORLD_TYPE else ""
    try:
        return gc.is_truthy(gc.get(db_session, group_id, f"{prefix}{SAFE_DEATH_CONFIG_KEY}"))
    except Exception:
        return False


def death_skip_reason(db_session, group_id, notification_type, data) -> str | None:
    """Why this group must not be told about this death, or ``None``.

    Returns a short greppable reason, matching ``blacklist_reason``, so a
    withheld notification is legible on its ``notification_queue`` row rather
    than just missing.
    """
    if not group_id or notification_type not in FILTERABLE_TYPES:
        return None
    if not isinstance(data, dict):
        return None
    if not is_safe_death(data):
        return None
    if announces_safe_deaths(db_session, group_id, data.get("world_type")):
        return None
    where = data.get("region_name") or data.get("location")
    return f"safe death ({where})" if where else "safe death"
