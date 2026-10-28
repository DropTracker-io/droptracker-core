"""The shared "what a group can switch on" catalog (db/group_features.py),
the members' guide wording (utils/member_guide.py) and the Web API routes
that serve and enable them (web_api/routes/group_setup.py)."""

import asyncio

import pytest

from db import group_features as gf
from utils import member_guide
from web_api.config_registry import get_config_field


def test_every_feature_has_a_known_kind_and_a_group_path():
    keys = [f["key"] for f in gf.FEATURES]
    assert len(keys) == len(set(keys))
    for f in gf.FEATURES:
        assert f["kind"] in ("toggle", "channel", "link"), f["key"]
        assert f["path"].startswith("/groups/{id}/"), f["key"]
        assert f["title"] and f["blurb"], f["key"]


def test_channel_and_switch_keys_are_real_registry_fields():
    for f in gf.FEATURES:
        if f.get("channel_key"):
            field = get_config_field(f["channel_key"])
            assert field is not None and field["type"] == "channel", f["key"]
        for key in f.get("enable") or {}:
            if key == gf.POINTS_ENABLED_KEY:
                continue  # owned by the points settings route, not the registry
            field = get_config_field(key)
            assert field is not None and field["type"] == "boolean", (f["key"], key)


def test_enableable_features_have_something_to_write():
    for f in gf.FEATURES:
        if f["kind"] == "toggle":
            assert f.get("enable"), f["key"]
        if f["kind"] == "channel":
            assert f.get("channel_key"), f["key"]


def test_entitlement_keys_exist():
    from db.entitlements import all_entitlement_keys

    known = set(all_entitlement_keys("group"))
    for f in gf.FEATURES:
        if f.get("entitlement"):
            assert f["entitlement"] in known, f["key"]


def test_enable_updates_toggle_channel_and_link():
    assert gf.enable_updates("clan_points") == {gf.POINTS_ENABLED_KEY: True}
    assert gf.enable_updates("clan_log", "123456789012345678") == {
        "clan_log_enabled": True, "clan_log_channel_id": "123456789012345678"}
    with pytest.raises(ValueError):
        gf.enable_updates("clan_log")  # needs a channel
    with pytest.raises(ValueError):
        gf.enable_updates("events")  # set up on its own page
    with pytest.raises(KeyError):
        gf.enable_updates("nope")


@pytest.mark.parametrize("cfg,expected", [
    ({}, False),
    ({"clan_log_channel_id": "123"}, False),  # switch off
    ({"clan_log_enabled": "1", "clan_log_channel_id": "0"}, False),  # legacy unset
    ({"clan_log_enabled": "1", "clan_log_channel_id": "123"}, True),
])
def test_channel_feature_is_on_only_with_switch_and_channel(cfg, expected):
    assert gf._is_on(None, 1, gf.get_feature("clan_log"), cfg) is expected


def test_toggle_and_plain_channel_status():
    assert gf._is_on(None, 1, gf.get_feature("clan_points"), {"points_enabled": "1"})
    assert not gf._is_on(None, 1, gf.get_feature("clan_points"), {"points_enabled": "0"})
    assert gf._is_on(None, 1, gf.get_feature("lootboard"), {"lootboard_channel_id": "9"})
    assert not gf._is_on(None, 1, gf.get_feature("lootboard"), {})


def test_free_tier_features_falls_back_when_no_tier_row():
    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    class _S:
        def query(self, *_a):
            return _Q()

    assert gf.free_tier_features(_S()) == gf._FALLBACK_FREE_FEATURES


def _all_copy():
    yield member_guide.TITLE
    yield member_guide.intro("Clan")
    for h, b in member_guide.sections():
        yield h
        yield b
    for f in gf.FEATURES:
        yield f["title"]
        yield f["blurb"]


def test_user_facing_copy_has_no_em_dashes():
    # Site copy style: em-dashes read as AI-written.
    for text in _all_copy():
        assert "—" not in text, text


def test_member_guide_embed_and_buttons():
    embed = member_guide.guide_embed("Iron Lads")
    assert "Iron Lads" in embed["description"]
    assert len(embed["fields"]) == len(member_guide.sections())
    assert all(len(f["value"]) <= 1024 for f in embed["fields"])
    labels = [b["label"] for b in member_guide.guide_buttons(42)]
    assert labels == ["Get the plugin", "Link your account", "Our clan page"]
    assert all(b["url"].startswith("https://") for b in member_guide.guide_buttons(42))
    assert len(member_guide.guide_buttons(None)) == 2


def test_member_guide_mentions_use_the_callback():
    text = " ".join(b for _h, b in member_guide.sections(lambda n: f"<{n}>"))
    assert "<claim-rsn>" in text and "`/claim-rsn`" not in text
    plain = " ".join(b for _h, b in member_guide.sections())
    assert "`/claim-rsn`" in plain


# --- Web API ------------------------------------------------------------------
def test_enable_unknown_feature_is_404():
    from web_api.common import ProblemException
    from web_api.routes import group_setup

    with pytest.raises(ProblemException) as e:
        group_setup.enable_group_feature(1, 2, "nope", None)
    assert e.value.status == 404


def test_enable_link_feature_is_422():
    from web_api.common import ProblemException
    from web_api.routes import group_setup

    with pytest.raises(ProblemException) as e:
        group_setup.enable_group_feature(1, 2, "events", None)
    assert e.value.status == 422


def test_enable_channel_feature_goes_through_the_settings_writer(monkeypatch):
    from web_api.routes import config, group_setup

    calls = []
    monkeypatch.setattr(config, "apply_group_config_patch",
                        lambda uid, gid, body: calls.append((uid, gid, body)))
    group_setup.enable_group_feature(7, 9, "recaps", "123456789012345678")
    assert calls == [(7, 9, {"recaps_enabled": True,
                             "channel_id_to_post_recaps": "123456789012345678"})]
