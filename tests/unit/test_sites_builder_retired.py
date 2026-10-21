"""web137a: the clan site builder is retired; every address is a redirect."""
from types import SimpleNamespace

from web_api.routes.sites import SELECTABLE_SITE_MODES, _effective_mode, _resolved_redirect_target


def _site(mode, redirect_url=None, group_id=14):
    return SimpleNamespace(mode=mode, redirect_url=redirect_url, group_id=group_id)


def test_builder_sites_behave_as_group_page(monkeypatch):
    monkeypatch.setenv("WEB_SITE_URL", "https://www.droptracker.io/")
    for mode in ("builder", None, ""):
        site = _site(mode)
        assert _effective_mode(site) == "group_page"
        assert _resolved_redirect_target(site) == "https://www.droptracker.io/groups/14"


def test_redirect_modes_unchanged(monkeypatch):
    monkeypatch.setenv("WEB_SITE_URL", "https://www.droptracker.io")
    assert _resolved_redirect_target(_site("redirect", "https://discord.gg/x")) == "https://discord.gg/x"
    assert _resolved_redirect_target(_site("redirect", "  ")) is None
    assert _resolved_redirect_target(_site("group_page", group_id=7)) == "https://www.droptracker.io/groups/7"


def test_builder_is_no_longer_selectable():
    assert "builder" not in SELECTABLE_SITE_MODES
    assert set(SELECTABLE_SITE_MODES) == {"group_page", "redirect"}
