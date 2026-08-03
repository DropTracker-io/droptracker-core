"""Default (no DB template) embeds link to the current site.

These builders run whenever a group has no custom embed for the submission
type, so they are the fallback the majority of groups actually see. They each
render a player profile link, which used to be hand-built in the XenForo shape
(``/players/{Name}.{id}/view``) and now comes from ``utils.site_urls``.

The test exercises the builders rather than asserting on the URL string alone:
a module-level helper called ``player_link`` is trivially shadowed by a local of
the same name, which Python turns into an UnboundLocalError for the whole
function — a failure that neither ``py_compile`` nor a string grep catches.

Loaded directly from the file path (like test_notification_channel_guard.py)
because conftest stubs the ``services`` package.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

for _name in ("services.contribution_notifications", "services.event_notifications"):
    if _name not in sys.modules:
        sys.modules[_name] = MagicMock()

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "notification_service.py",
)
_spec = importlib.util.spec_from_file_location("_notification_default_embeds_under_test", _MODULE_PATH)
ns = importlib.util.module_from_spec(_spec)
sys.modules["_notification_default_embeds_under_test"] = ns
_spec.loader.exec_module(ns)

NotificationService = ns.NotificationService

PLAYER_NAME = "Zezima"
PLAYER_ID = 5
EXPECTED_LINK = "[Zezima](https://www.droptracker.io/players/5)"


@pytest.fixture
def service():
    return NotificationService(MagicMock(), MagicMock())


@pytest.fixture
def embed_descriptions(monkeypatch):
    """`interactions` is a conftest MagicMock, so read the description off the
    Embed(...) call rather than the returned mock's attribute."""
    seen = []

    def record(*args, **kwargs):
        seen.append(kwargs.get("description", ""))
        return MagicMock()

    monkeypatch.setattr(ns.interactions, "Embed", record)
    return seen


@pytest.mark.parametrize(
    "builder, data",
    [
        ("_build_default_quest_embed", {"quest_name": "Dragon Slayer", "quests_completed": 12}),
        ("_build_default_death_embed", {"source": "Vorkath", "location": "Ungael"}),
        ("_build_default_diary_embed", {"diary_name": "Karamja", "diary_tier": "Elite"}),
    ],
)
def test_default_embed_links_to_the_current_player_url(service, embed_descriptions, builder, data):
    getattr(service, builder)(data, PLAYER_NAME, PLAYER_ID)

    assert len(embed_descriptions) == 1
    description = embed_descriptions[0]
    assert EXPECTED_LINK in description
    # The XenForo shape and its /view action must be gone.
    assert f"{PLAYER_NAME}.{PLAYER_ID}" not in description
    assert "/view" not in description


def test_link_helpers_are_not_shadowed_in_the_module(service):
    # The bug this guards: `player_link = player_link(...)` inside a method
    # rebinds the name for the whole function body, so the call that produces
    # the value raises UnboundLocalError before the embed is ever built.
    assert callable(ns.player_link)
    assert callable(ns.group_link)
    assert ns.player_link(PLAYER_NAME, PLAYER_ID) == EXPECTED_LINK
