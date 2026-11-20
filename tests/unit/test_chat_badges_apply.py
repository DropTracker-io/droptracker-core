"""Bug #255: clan-chat team badges only where they tell rival teams apart."""
import importlib.util
import os
import sys

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "plugin_notifications.py",
)
_spec = importlib.util.spec_from_file_location("_plugin_notifications_badges", _PATH)
pn = importlib.util.module_from_spec(_spec)
sys.modules["_plugin_notifications_badges"] = pn
_spec.loader.exec_module(pn)


class _Ev:
    def __init__(self, tags=True, mode="admin_assign"):
        self.team_chat_tags = tags
        self.formation_mode = mode


def test_single_whole_clan_team_is_not_badged():
    assert pn.chat_badges_apply(_Ev(), 1) is False


def test_rival_teams_are_badged():
    assert pn.chat_badges_apply(_Ev(), 2) is True


def test_free_for_all_is_not_badged():
    assert pn.chat_badges_apply(_Ev(mode="solo"), 40) is False


def test_switched_off_is_not_badged():
    assert pn.chat_badges_apply(_Ev(tags=False), 3) is False
    assert pn.chat_badges_apply_for(0, "admin_assign", 3) is False
