"""What a group can switch on, and whether it has.

One catalog behind every "turn things on" surface: the website's setup wizard
step, the "Features you haven't tried" list on the admin Overview, the Discord
``/group-setup`` panel and the "What is DropTracker?" explainer. Each surface
used to describe the product in its own words (or not at all), so free
features such as clan points or the Clan Log were never offered to new groups.

Pure DB: no Quart, no interactions, so both the Web API and the bots import it.
Writes stay with the callers (``web_api.routes.config.apply_group_config_patch``
on the site, ``services.group_config_writer`` in Discord); this module only says
*what* to write via :func:`enable_updates`.

Feature kinds:

* ``toggle``: one switch, enabled from the surface itself.
* ``channel``: pick a channel (plus an optional switch), enabled in place.
* ``link``: needs more than a click; the surface links to the page for it.

Statuses: ``on`` | ``off`` | ``locked`` (the group's plan doesn't include it;
``tier`` names the cheapest plan that does).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

#: Config key behind clan points. Not a registry key: the points settings
#: route owns it (web_api/routes/points.py BEHAVIOR_BOOL_KEYS).
POINTS_ENABLED_KEY = "points_enabled"

FEATURES: List[Dict[str, Any]] = [
    {
        "key": "clan_points",
        "title": "Clan points",
        "blurb": "Members earn points for drops, PBs, pets and logs, with monthly and seasonal leaderboards.",
        "kind": "toggle",
        "enable": {POINTS_ENABLED_KEY: True},
        "path": "/groups/{id}/points",
    },
    {
        "key": "lootboard",
        "title": "Lootboard",
        "blurb": "A live image of the clan's top loot this month, kept up to date in one message.",
        "kind": "channel",
        "channel_key": "lootboard_channel_id",
        "path": "/groups/{id}/settings#cfg-board",
    },
    {
        "key": "clan_log",
        "title": "Clan Log",
        "blurb": "A shared collection log for the whole clan, posted as a live board.",
        "kind": "channel",
        "channel_key": "clan_log_channel_id",
        "enable": {"clan_log_enabled": True},
        "path": "/groups/{id}/settings#cfg-clan_log",
    },
    {
        "key": "recaps",
        "title": "Monthly recaps",
        "blurb": "A summary of the clan's month: top loot, PBs, pets and who played most.",
        "kind": "channel",
        "channel_key": "channel_id_to_post_recaps",
        "enable": {"recaps_enabled": True},
        "path": "/groups/{id}/settings#cfg-recaps",
    },
    {
        "key": "deaths",
        "title": "Death announcements",
        "blurb": "Post members' deaths, with your own messages (or theirs) for a laugh.",
        "kind": "channel",
        "channel_key": "channel_id_to_post_deaths",
        "enable": {"notify_deaths": True},
        "path": "/groups/{id}/settings#cfg-deaths",
    },
    {
        "key": "submit_card",
        "title": "Submit button",
        "blurb": "A card with a button members press to submit a drop the plugin missed.",
        "kind": "channel",
        "channel_key": "submit_panel_channel",
        "path": "/groups/{id}/settings#cfg-channels",
    },
    {
        "key": "activity_launcher",
        "title": "DropTracker Activity",
        "blurb": "Open the clan's boards, events and profiles inside Discord.",
        "kind": "channel",
        "channel_key": "activity_launch_channel",
        "path": "/groups/{id}/settings#cfg-channels",
    },
    {
        "key": "events",
        "title": "Events",
        "blurb": "Bingo, board games, Skill and Boss of the Week: scored automatically from the plugin.",
        "kind": "link",
        "path": "/groups/{id}/events/new",
    },
    {
        "key": "custom_embeds",
        "title": "Custom notification designs",
        "blurb": "Change how every message the bot posts looks.",
        "kind": "link",
        "entitlement": "custom_embeds",
        "path": "/groups/{id}/embeds",
    },
    {
        "key": "clan_chat_bridge",
        "title": "Clan chat bridge",
        "blurb": "Relay in-game clan chat to a Discord channel, and back.",
        "kind": "link",
        "path": "/groups/{id}/settings#cfg-clan_chat",
    },
    {
        "key": "voice_counters",
        "title": "Voice channel counters",
        "blurb": "Show the clan's monthly loot or member count as a channel name.",
        "kind": "link",
        "path": "/groups/{id}/settings#cfg-voice",
    },
    {
        "key": "hall_of_fame",
        "title": "Hall of Fame",
        "blurb": "Personal-best boards for the bosses you choose.",
        "kind": "link",
        "entitlement": "hall_of_fame",
        "path": "/groups/{id}/hall-of-fame",
    },
    {
        "key": "group_site",
        "title": "Clan website",
        "blurb": "Your clan's own site on osrs.site, built from blocks with live data.",
        "kind": "link",
        "entitlement": "custom_site",
        "path": "/groups/{id}/website",
    },
]

_BY_KEY = {f["key"]: f for f in FEATURES}

#: Every config key a status check reads.
_STATUS_KEYS = sorted({
    POINTS_ENABLED_KEY, "create_pb_embeds", "channel_id_to_send_pb_embeds",
    "clan_chat_bridge_enabled", "channel_id_clan_chat_bridge",
    "vc_to_display_monthly_loot", "vc_to_display_droptracker_users",
    *(f["channel_key"] for f in FEATURES if f.get("channel_key")),
    *(k for f in FEATURES for k in (f.get("enable") or {})),
})


def get_feature(key: str) -> Optional[Dict[str, Any]]:
    return _BY_KEY.get(key)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _channel_set(v) -> bool:
    # Legacy "0" means unset (config-zero-sentinels).
    v = str(v or "").strip()
    return bool(v) and v != "0"


def _config_values(s, group_id: int) -> Dict[str, str]:
    from db.models import GroupConfiguration

    rows = (
        s.query(GroupConfiguration)
        .filter(GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key.in_(_STATUS_KEYS))
        .order_by(GroupConfiguration.id.desc())
        .all()
    )
    # Descending + overwrite = lowest id wins, as every reader resolves it.
    out: Dict[str, str] = {}
    for r in rows:
        out[r.config_key] = r.long_value if (r.long_value and not r.config_value) else (r.config_value or "")
    return out


def _is_on(s, group_id: int, feature: Dict[str, Any], cfg: Dict[str, str]) -> bool:
    key = feature["key"]
    if key == "events":
        from db.models.events import Event

        return s.query(Event.id).filter(Event.group_id == group_id).first() is not None
    if key == "custom_embeds":
        from db.models.embed import GroupEmbed

        return s.query(GroupEmbed.embed_id).filter(GroupEmbed.group_id == group_id).first() is not None
    if key == "clan_chat_bridge":
        return _truthy(cfg.get("clan_chat_bridge_enabled")) and _channel_set(cfg.get("channel_id_clan_chat_bridge"))
    if key == "voice_counters":
        return _channel_set(cfg.get("vc_to_display_monthly_loot")) or _channel_set(cfg.get("vc_to_display_droptracker_users"))
    if key == "hall_of_fame":
        return _truthy(cfg.get("create_pb_embeds")) and _channel_set(cfg.get("channel_id_to_send_pb_embeds"))
    if key == "group_site":
        return False  # the page shows its own state; never nag about it
    enabled = all(_truthy(cfg.get(k)) for k in (feature.get("enable") or {}))
    channel_ok = _channel_set(cfg.get(feature["channel_key"])) if feature.get("channel_key") else True
    return enabled and channel_ok


def _cheapest_tier_names(s) -> Dict[str, str]:
    """{entitlement key: name of the cheapest paid group tier granting it}."""
    from db.entitlements import parse_stored_entitlements, resolve_tier_entitlements
    from db.models.subscriptions import SubscriptionTier

    tiers = (
        s.query(SubscriptionTier)
        .filter(SubscriptionTier.active == True,  # noqa: E712
                SubscriptionTier.scope == "group",
                SubscriptionTier.price_cents > 0)
        .order_by(SubscriptionTier.price_cents.asc())
        .all()
    )
    out: Dict[str, str] = {}
    for t in tiers:
        ents = resolve_tier_entitlements(parse_stored_entitlements(t.entitlements, "group"), "group")
        for k, v in ents.items():
            if v and k not in out:
                out[k] = t.name
    return out


def feature_status(s, group_id: int) -> List[Dict[str, Any]]:
    """Every feature with this group's status, in catalog order."""
    from db.entitlements import resolve_group_entitlements

    cfg = _config_values(s, group_id)
    ents = resolve_group_entitlements(s, group_id)
    tier_names = None
    out = []
    for f in FEATURES:
        ent = f.get("entitlement")
        locked = bool(ent) and not ents.get(ent)
        if locked:
            if tier_names is None:
                tier_names = _cheapest_tier_names(s)
            status = "locked"
        else:
            status = "on" if _is_on(s, group_id, f, cfg) else "off"
        out.append({
            "key": f["key"],
            "title": f["title"],
            "blurb": f["blurb"],
            "kind": f["kind"],
            "status": status,
            "tier": (tier_names or {}).get(ent) if locked else None,
            "channel_key": f.get("channel_key"),
            "path": f["path"].format(id=group_id),
        })
    return out


def enable_updates(key: str, channel_id: Optional[str] = None) -> Dict[str, Any]:
    """Raw {config key: value} that switches feature ``key`` on.

    Raises KeyError for an unknown feature and ValueError when the feature
    can't be enabled in place (``link`` kind) or needs a channel and got none.
    Registry keys still go through the caller's validation; ``points_enabled``
    is written as-is (see :data:`POINTS_ENABLED_KEY`)."""
    f = _BY_KEY[key]
    if f["kind"] == "link":
        raise ValueError(f"'{key}' is set up on its own page.")
    updates: Dict[str, Any] = dict(f.get("enable") or {})
    if f.get("channel_key"):
        if not channel_id:
            raise ValueError("Pick a channel first.")
        updates[f["channel_key"]] = str(channel_id)
    return updates


#: Used when the free tier row is missing or has no feature list.
_FALLBACK_FREE_FEATURES = [
    "Discord notifications for drops, collection logs, personal bests, pets and more",
    "Custom notification designs for every message the bot posts",
    "Clan points: award rules, item and boss overrides, timed boosts and leaderboards",
    "Events: bingo, board games, Skill and Boss of the Week",
    "A lootboard posted to Discord",
]


def free_tier_features(s) -> List[str]:
    """The Free plan's marketing lines (the same list as the /premium Free
    card), so the bot's explainer can't drift from the website."""
    from db.entitlements import _FALLBACK_TIER_KEYS
    from db.models.subscriptions import SubscriptionTier

    for tier_key in _FALLBACK_TIER_KEYS:
        tier = (
            s.query(SubscriptionTier)
            .filter(SubscriptionTier.key == tier_key, SubscriptionTier.active == True)  # noqa: E712
            .first()
        )
        if tier is None or not tier.features:
            continue
        try:
            lines = json.loads(tier.features)
        except Exception:
            continue
        lines = [str(x).strip() for x in lines if str(x).strip()] if isinstance(lines, list) else []
        if lines:
            return lines
    return list(_FALLBACK_FREE_FEATURES)
