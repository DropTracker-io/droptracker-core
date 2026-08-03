"""Canonical droptracker.io links for entities we mention in Discord.

The pre-2026 site was XenForo, which addressed every entity as ``{Title}.{id}``
and hung the action off the end — ``/groups/PlayTheGame.176/view``,
``/players/Zezima.5/view``. The current site routes ``/{kind}/{id-or-slug}``
instead, so links built the old way only survive on a redirect (and the
hand-written ones that never carried an id, like ``/groups/upgrades``, do not
survive at all). Build links through this module so there is one place to change
if the routes move again.

Links are built from the **id**, not from a name-derived slug. The id is exact;
a slug lands on the site's disambiguation page whenever two groups or players
share a name, and the bot cannot know whether a name is unique without a query
it would have to make per message. Nothing is lost by it — the reader sees the
markdown label, not the URL, and the site emits the pretty slug as the page's
canonical URL for crawlers.

Deliberately pure: stdlib only, no DB and no network, so it is importable from
the bots, the notification service and the tests alike.
"""

WEBSITE_URL = "https://www.droptracker.io"

#: Where a "give us money" prompt should point. XF had per-group upgrade pages
#: under /groups/{ref}/upgrades and a personal one under /account/premium; both
#: are now the single public /premium page (per-group management lives on the
#: group's own admin page, see :func:`group_subscription_url`).
PREMIUM_URL = f"{WEBSITE_URL}/premium"


def player_url(player_id) -> str:
    """Public profile page for a player."""
    return f"{WEBSITE_URL}/players/{player_id}"


def group_url(group_id) -> str:
    """Public profile page for a group."""
    return f"{WEBSITE_URL}/groups/{group_id}"


def npc_url(npc_id) -> str:
    """Public NPC page (drop table, loot totals, personal-best boards)."""
    return f"{WEBSITE_URL}/npcs/{npc_id}"


def item_url(item_id) -> str:
    """Public item page (recent receivers, top collectors, drop sources)."""
    return f"{WEBSITE_URL}/items/{item_id}"


def event_url(event_id) -> str:
    """Public event page."""
    return f"{WEBSITE_URL}/events/{event_id}"


def group_subscription_url(group_id) -> str:
    """A group's subscription tab — where an admin actually manages the plan."""
    return f"{WEBSITE_URL}/groups/{group_id}/subscription"


def group_submissions_url(group_id) -> str:
    """A group's manual-submission review queue."""
    return f"{WEBSITE_URL}/groups/{group_id}/submissions"


def player_link(player_name, player_id) -> str:
    """Discord-markdown link to a player's profile.

    The label is the name as given — Discord renders `[label](url)` and callers
    have already decided how the name should read.
    """
    return f"[{player_name}]({player_url(player_id)})"


def group_link(group_name, group_id) -> str:
    """Discord-markdown link to a group's profile."""
    return f"[{group_name}]({group_url(group_id)})"


def npc_link(npc_name, npc_id) -> str:
    """Discord-markdown link to an NPC page."""
    return f"[{npc_name}]({npc_url(npc_id)})"
