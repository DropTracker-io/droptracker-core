"""Which in-game clans DropTracker accepts relayed clan chat for.

Clan chat relaying is on by default in the plugin (6.0.18+), so the privacy
line is drawn on the server's side of the wire: a clan's chat, and the
broadcasts in its chat box, are only taken in when at least one group has
opted that clan in, through ``clan_chat_bridge_enabled`` plus a bridge channel
(the two-way bridge) or through ``clan_broadcast_tracking``. Both need
``clan_chat_name`` to name the clan.

Three layers enforce it, so no single bug stores a stranger's chat:

1. the plugin only relays when one of the player's OWN groups opted their
   current clan in (``/load_config`` / ``/panel_data`` expose
   :func:`group_relay_fields`), so a non-participant's client sends nothing;
2. the intake acceptor drops relay payloads for clans no group opted in
   (:func:`relay_payload_unwanted`), before anything is queued;
3. the processors bind each line to the RELAYER's own opted-in groups and
   discard everything else without writing it anywhere.

Stdlib-only at import time; the DB read is lazy and cached per process.
"""

from __future__ import annotations

import time

BRIDGE_ENABLED_KEY = "clan_chat_bridge_enabled"
BRIDGE_CHANNEL_KEY = "channel_id_clan_chat_bridge"
TRACKING_KEY = "clan_broadcast_tracking"
CLAN_NAME_KEY = "clan_chat_name"

#: Submission types carrying raw clan chat text.
RELAY_TYPES = frozenset({"clan_chat", "clan_broadcast"})

_CACHE_TTL_SECONDS = 60
_cache = {"expires": 0.0, "slugs": None}


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _channel_set(value) -> bool:
    return str(value or "").strip() not in ("", "0", "false", "False")


def relay_flags(values: dict) -> tuple:
    """``(bridge, tracking, slug)`` for one group's config values.

    ``values`` maps config key → raw stored string. ``slug`` is the clan's
    comparison key (``utils.clan_broadcasts.clan_slug``), "" when unnamed."""
    from utils.clan_broadcasts import clan_slug

    slug = clan_slug(values.get(CLAN_NAME_KEY) or "")
    if not slug:
        return False, False, ""
    bridge = _truthy(values.get(BRIDGE_ENABLED_KEY)) and _channel_set(
        values.get(BRIDGE_CHANNEL_KEY)
    )
    tracking = _truthy(values.get(TRACKING_KEY))
    return bridge, tracking, slug


def group_relay_fields(values: dict) -> dict:
    """The fields the plugin's group config carries for this feature.

    ``clan_chat_slug`` is only sent while the group actually uses one of the
    two features, so the plugin's "is my clan opted in?" check cannot match a
    clan name a group set and then switched off."""
    bridge, tracking, slug = relay_flags(values)
    return {
        "clan_chat_bridge": bridge,
        "clan_broadcast_tracking": tracking,
        "clan_chat_slug": slug if (bridge or tracking) else "",
    }


def opted_in_clan_slugs(session=None) -> frozenset:
    """Slugs of every clan at least one group opted in, cached for 60s.

    Returns None when the set could not be read, so the caller can fail open
    (the processors still discard unbound lines)."""
    now = time.monotonic()
    if _cache["slugs"] is not None and now < _cache["expires"]:
        return _cache["slugs"]
    owns = session is None
    try:
        from db.models import GroupConfiguration, Session

        if owns:
            session = Session()
        rows = (
            session.query(
                GroupConfiguration.group_id,
                GroupConfiguration.config_key,
                GroupConfiguration.config_value,
                GroupConfiguration.long_value,
            )
            .filter(
                GroupConfiguration.config_key.in_(
                    [BRIDGE_ENABLED_KEY, BRIDGE_CHANNEL_KEY, TRACKING_KEY, CLAN_NAME_KEY]
                )
            )
            .all()
        )
        by_group: dict = {}
        for gid, key, value, long_value in rows:
            by_group.setdefault(gid, {})[key] = value if value not in (None, "") else long_value
        slugs = set()
        for values in by_group.values():
            bridge, tracking, slug = relay_flags(values)
            if slug and (bridge or tracking):
                slugs.add(slug)
        _cache["slugs"] = frozenset(slugs)
        _cache["expires"] = now + _CACHE_TTL_SECONDS
        return _cache["slugs"]
    except Exception as e:
        print(f"[ClanRelayGate] opted-in clan refresh failed: {e}")
        return _cache["slugs"]
    finally:
        if owns and session is not None:
            try:
                session.close()
            except Exception:
                pass


def _embed_fields(embed) -> dict:
    fields = {}
    for field in (embed or {}).get("fields") or []:
        if isinstance(field, dict) and field.get("name"):
            fields[str(field["name"])] = field.get("value")
    return fields


def relay_payload_unwanted(webhook_payload, opted_in=None) -> bool:
    """Whether an intake payload is clan chat for a clan nobody opted in.

    True only when EVERY embed is a relay type and none names an opted-in
    clan, so a mixed or unrecognized payload is never dropped here. Fails open
    when the opted-in set is unavailable."""
    embeds = (webhook_payload or {}).get("embeds") if isinstance(webhook_payload, dict) else None
    if not embeds:
        return False
    parsed = [_embed_fields(e) for e in embeds]
    if not all(str(f.get("type") or "").strip().lower() in RELAY_TYPES for f in parsed):
        return False
    if opted_in is None:
        opted_in = opted_in_clan_slugs()
    if opted_in is None:
        return False
    from utils.clan_broadcasts import clan_slug

    return not any(clan_slug(f.get("clan_name") or "") in opted_in for f in parsed)
