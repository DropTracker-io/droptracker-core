"""What ``/personal-bests/<id>/loadout`` says about the character model (web110a).

The loadout row names an outfit fingerprint; whether anything renderable still
exists under it is a storage question. The block answers it so the site never
mounts a 3D viewer on a model that has been pruned — and still shows the
pre-rendered still when only that remains, because renders are never pruned.

``services.player_model`` / ``services.gear_image`` are the conftest-loaded
real modules, monkeypatched per test to a fake store.
"""
from __future__ import annotations

import sys

import pytest

import web_api.routes.player_state as ps

player_model = sys.modules["services.player_model"]
gear_image = sys.modules["services.gear_image"]


@pytest.fixture
def store(monkeypatch):
    """The set of (fingerprint, kind) objects the fake storage holds."""
    present = set()
    monkeypatch.setattr(
        player_model, "model_exists",
        lambda pid, fp, pet=False: (fp, "pet" if pet else "model") in present,
    )
    monkeypatch.setattr(gear_image, "image_exists", lambda pid, fp: (fp, "image") in present)
    monkeypatch.setattr(gear_image, "image_url", lambda pid, fp: f"https://cdn/{pid}/{fp}.png")
    return present


class TestModelBlock:
    def test_no_fingerprint_means_no_block(self, store):
        assert ps.pb_model_block(5, None, "kill") is None
        assert ps.pb_model_block(5, "", "kill") is None

    def test_model_and_still_both_present(self, store):
        store.update({("abcd", "model"), ("abcd", "image")})
        assert ps.pb_model_block(5, "abcd", "kill") == {
            "player_id": 5,
            "fingerprint": "abcd",
            "source": "kill",
            "has_model": True,
            "has_pet": False,
            "image_url": "https://cdn/5/abcd.png",
        }

    def test_a_pet_is_only_reported_beside_a_model(self, store):
        store.update({("abcd", "model"), ("abcd", "pet")})
        block = ps.pb_model_block(5, "abcd", "kill")
        assert block["has_pet"] is True
        assert block["image_url"] is None

    def test_a_pruned_model_falls_back_to_its_still(self, store):
        store.add(("abcd", "image"))
        block = ps.pb_model_block(5, "abcd", "recent")
        assert block["has_model"] is False and block["has_pet"] is False
        assert block["image_url"] == "https://cdn/5/abcd.png"

    def test_nothing_renderable_means_no_block(self, store):
        assert ps.pb_model_block(5, "abcd", "kill") is None

    def test_rows_without_a_source_are_never_called_exact(self, store):
        store.add(("abcd", "model"))
        assert ps.pb_model_block(5, "abcd", None)["source"] == "recent"

    def test_a_malformed_fingerprint_never_reaches_storage(self, store, monkeypatch):
        def boom(*_a, **_k):
            raise AssertionError("storage was consulted")

        monkeypatch.setattr(player_model, "model_exists", boom)
        monkeypatch.setattr(gear_image, "image_exists", boom)
        assert ps.pb_model_block(5, "../etc/passwd", "kill") is None

    def test_a_storage_failure_costs_the_picture_not_the_loadout(self, store, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("B2 down")

        monkeypatch.setattr(player_model, "model_exists", boom)
        assert ps.pb_model_block(5, "abcd", "kill") is None
