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


# The staff editor (web_api/routes/notification_defaults.py) shows these three
# builders as templates, so an admin can see and start from what groups are
# sent. A template cannot branch, so only what it CAN say is held equal here:
# title, colour, the description with its values filled in, and the field
# names when every value is present.
FULL_DATA = {
    "quest": {
        "quest_name": "Dragon Slayer", "quests_completed": 12, "total_quests": 170,
        "completion_percentage": "7%", "quest_points": 2, "total_quest_points": 40,
        "qp_percentage": "12%",
    },
    "death": {"source": "Vorkath", "region_name": "Ungael", "value_lost": 4_200_000},
    "diary": {"diary_name": "Karamja", "diary_tier": "Elite"},
}


@pytest.mark.parametrize("embed_type", ["quest", "death", "diary"])
def test_staff_editor_builtins_follow_the_builders(service, monkeypatch, embed_type):
    from web_api.routes.notification_defaults import BUILTIN_EMBEDS

    built = []

    def record(*args, **kwargs):
        embed = MagicMock()
        built.append((kwargs, embed))
        return embed

    monkeypatch.setattr(ns.interactions, "Embed", record)
    builder = getattr(service, f"_build_default_{embed_type}_embed")
    builder(FULL_DATA[embed_type], PLAYER_NAME, PLAYER_ID, video_url="https://x.test/v.mp4")

    ((kwargs, embed),) = built
    template = BUILTIN_EMBEDS[embed_type]
    assert template["title"] == kwargs["title"]
    assert template["color"].lower() == kwargs["color"].lower()

    values = {"{player_name}": EXPECTED_LINK}
    values.update({f"{{{k}}}": str(v) for k, v in FULL_DATA[embed_type].items()})
    description = template["description"]
    for token, value in values.items():
        description = description.replace(token, value)
    assert description == kwargs["description"]

    names = [c.kwargs["name"] for c in embed.add_field.call_args_list]
    assert [f["name"] for f in template["fields"]] == names
