"""Record a new collection log slot as a drop for items that never arrive as one.

Some rare items reach the collection log without any loot event the plugin
could submit, so the drop leaderboards and loot totals never see them. The
amulet of eternal glory is the case this exists for: a 1/25,000 roll when
charging glories at the Fountain of Rune, announced only by a game message.

When clog_processor records a NEW slot for one of these items, we submit a
matching drop through drop_processor, priced server-side (the payload carries
value 0, so get_true_item_value uses the GE price). The drop guid is derived
from the clog guid, so a replayed clog cannot record a second drop.

Only a new slot triggers this, so a player's second eternal glory (the game
shows no new-slot message for it) is still not recorded.
"""

import uuid

# item_id -> drop source. Each source needs an npc_list row
# (scripts/seed_activity_sources.py) and an ACTIVITY_ITEM_SOURCES pair in
# osrs_api/semantic.py so the >1M wiki verification accepts it.
CLOG_DROP_SOURCES = {
    19707: "Fountain of Rune",  # Amulet of eternal glory
}

# Clog-only payload keys that drop_processor would misread or that point at
# the clog's own attachment.
_CLOG_ONLY_KEYS = ("item", "kc", "attachment_url", "attachment_type", "downloaded", "image_url")


def mirror_drop_guid(clog_guid: str) -> str:
    """Stable drop guid for a clog guid."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"droptracker:clog-drop:{clog_guid}"))


def build_mirror_drop(clog_data: dict, item_id: int, item_name: str, image_url: str | None):
    """drop_processor payload for a new clog slot, or None when this item
    isn't mirrored (or the clog has no guid to derive a replay-safe one)."""
    source = CLOG_DROP_SOURCES.get(item_id)
    clog_guid = clog_data.get("guid")
    if source is None or not clog_guid:
        return None
    # Start from the clog payload so auth, account type, plugin version,
    # intake source and the accept-time stamp carry over unchanged.
    drop_data = {k: v for k, v in clog_data.items() if k not in _CLOG_ONLY_KEYS}
    drop_data.update({
        "type": "drop",
        "source": source,
        "item_id": item_id,
        "item_name": item_name,
        "quantity": 1,
        "value": 0,
        "guid": mirror_drop_guid(clog_guid),
    })
    if image_url:
        # The clog screenshot is already stored; reuse it rather than
        # downloading the attachment a second time.
        drop_data["downloaded"] = True
        drop_data["image_url"] = image_url
    return drop_data


async def mirror_clog_as_drop(clog_data, item_id, item_name, image_url,
                              external_session=None, world_type="main"):
    """Submit the mirrored drop. Never raises: the clog is already recorded."""
    try:
        drop_data = build_mirror_drop(clog_data, item_id, item_name, image_url)
        if drop_data is None:
            return None
        from .drop import drop_processor

        print(f"[ClogDropMirror] recording {item_name} for "
              f"{drop_data.get('player_name', drop_data.get('player'))} as a drop "
              f"from {drop_data['source']}")
        return await drop_processor(drop_data, external_session=external_session,
                                    world_type=world_type)
    except Exception as e:
        print(f"[ClogDropMirror] failed for {item_name} (item_id={item_id}): {e}")
        # drop_processor leaves a caller-owned session as it failed; clear it
        # so the clog's own notifications can still commit.
        try:
            if external_session is not None:
                external_session.rollback()
            else:
                from .common import session as global_session
                global_session.rollback()
        except Exception:
            pass
        return None
