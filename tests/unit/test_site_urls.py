"""Links we hand to Discord readers point at the current site, not XenForo.

The old site addressed entities as ``{Title}.{id}`` with the action on the end
(``/groups/PlayTheGame.176/view``). Those links only survive on a redirect now,
so nothing in the bot should build one — see ``utils/site_urls.py``.
"""
import re

import pytest

from utils.site_urls import (
    PREMIUM_URL,
    WEBSITE_URL,
    group_link,
    group_submissions_url,
    group_subscription_url,
    group_url,
    item_url,
    npc_link,
    npc_url,
    player_link,
    player_url,
    event_url,
)

#: The two shapes the old site used and the current one must never produce.
_XF_REF = re.compile(r"/[A-Za-z0-9_%-]+\.\d+(?:/|$)")
_XF_ACTION = re.compile(r"/view/?$")


@pytest.mark.parametrize(
    "url, expected",
    [
        (player_url(5), f"{WEBSITE_URL}/players/5"),
        (group_url(176), f"{WEBSITE_URL}/groups/176"),
        (npc_url(8060), f"{WEBSITE_URL}/npcs/8060"),
        (item_url(20997), f"{WEBSITE_URL}/items/20997"),
        (event_url(42), f"{WEBSITE_URL}/events/42"),
        (group_subscription_url(176), f"{WEBSITE_URL}/groups/176/subscription"),
        (group_submissions_url(176), f"{WEBSITE_URL}/groups/176/submissions"),
    ],
)
def test_entity_urls_use_the_current_id_routes(url, expected):
    assert url == expected


@pytest.mark.parametrize(
    "url",
    [
        player_url(5),
        group_url(176),
        npc_url(8060),
        item_url(20997),
        event_url(42),
        group_subscription_url(176),
        PREMIUM_URL,
    ],
)
def test_no_url_carries_a_xenforo_ref_or_action(url):
    assert not _XF_REF.search(url), f"{url} still looks like a XenForo ref"
    assert not _XF_ACTION.search(url), f"{url} still carries the XenForo /view action"


def test_links_are_discord_markdown_over_the_id_url():
    assert player_link("Zezima", 5) == f"[Zezima]({WEBSITE_URL}/players/5)"
    assert group_link("PlayTheGame", 176) == f"[PlayTheGame]({WEBSITE_URL}/groups/176)"
    assert npc_link("Vorkath", 8060) == f"[Vorkath]({WEBSITE_URL}/npcs/8060)"


def test_link_label_keeps_the_name_verbatim():
    # The URL is built from the id, so a name with spaces or punctuation needs
    # no escaping and no slug — it only ever appears as the label.
    assert player_link("Iron Zezima", 5) == f"[Iron Zezima]({WEBSITE_URL}/players/5)"
    assert group_link("Mr. Fluffy's Clan", 9) == f"[Mr. Fluffy's Clan]({WEBSITE_URL}/groups/9)"


def test_premium_url_is_the_live_upgrade_page():
    # XF served upgrades from /groups/{ref}/upgrades and /account/premium; both
    # 404 now, and the hand-written /groups/upgrades never existed at all.
    assert PREMIUM_URL == f"{WEBSITE_URL}/premium"
