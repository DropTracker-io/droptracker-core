"""Tests for the pure half of ``services.event_board_image`` — the visual-board
dispatch, the render-cache state hash, and the export-page URL builder. No DB,
no chromium, no network.

The conftest stubs the ``services`` package, so the real module loads by file
path (its only heavy import, ``db.app_logger``, is a harmless stub)."""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    name = "services.event_board_image"
    if name in sys.modules and getattr(sys.modules[name], "__file__", None):
        return sys.modules[name]
    path = _ROOT / "services" / "event_board_image.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load()


def _event(**kw):
    base = {"id": 7, "has_bingo": False, "kind": "standard"}
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Dispatch — which events have a visual board
# --------------------------------------------------------------------------- #
def test_standard_event_has_no_visual_board():
    # No DB is touched: the dispatch returns None before any query.
    assert m._collect_render_inputs(object(), _event()) is None


def test_dispatch_recognizes_bingo_and_board_game(monkeypatch):
    calls = {}
    monkeypatch.setattr(m, "_bingo_signature",
                        lambda s, e, team_id=None: {"kind": "bingo", "x": 1})
    monkeypatch.setattr(m, "_board_game_signature",
                        lambda s, e, team_id=None: {"kind": "board_game", "y": 2})
    bingo = m._collect_render_inputs(object(), _event(has_bingo=True, kind="bingo"))
    board = m._collect_render_inputs(object(), _event(has_bingo=False, kind="board_game"))
    assert bingo["kind"] == "bingo" and bingo["hash_src"]["x"] == 1
    assert board["kind"] == "board_game" and board["hash_src"]["y"] == 2
    # A signature of None (e.g. bingo flag but no cells yet) → no visual board.
    monkeypatch.setattr(m, "_bingo_signature", lambda s, e, team_id=None: None)
    assert m._collect_render_inputs(object(), _event(has_bingo=True)) is None


# --------------------------------------------------------------------------- #
# State hash — cache invalidation key
# --------------------------------------------------------------------------- #
def test_state_hash_is_order_independent_and_change_sensitive():
    a = m._state_hash({"hash_src": {"a": 1, "b": [3, 2, 1]}})
    b = m._state_hash({"hash_src": {"b": [3, 2, 1], "a": 1}})
    c = m._state_hash({"hash_src": {"a": 2, "b": [3, 2, 1]}})
    assert a == b        # key order doesn't matter (sort_keys)
    assert a != c        # a real value change flips the hash


def test_hash_changes_when_a_bingo_cell_is_completed():
    before = {"kind": "bingo", "hash_src": {
        "kind": "bingo", "cells": [(0, "A", 1, []), (1, "B", 2, [])]}}
    after = {"kind": "bingo", "hash_src": {
        "kind": "bingo", "cells": [(0, "A", 1, [10]), (1, "B", 2, [])]}}
    assert m._state_hash(before) != m._state_hash(after)


# --------------------------------------------------------------------------- #
# Export-page URL builder
# --------------------------------------------------------------------------- #
def test_board_image_page_url(monkeypatch):
    monkeypatch.setattr(m, "BOARD_IMAGE_BASE_URL", "https://www.droptracker.io")
    monkeypatch.setenv("BOARD_IMAGE_TOKEN", "s3cr et/tok")   # exercises url-quoting
    url = m.board_image_page_url(42)
    assert url.startswith("https://www.droptracker.io/board-image/42?k=")
    assert " " not in url and "/tok" not in url  # space + slash are percent-encoded
    assert "s3cr%20et%2Ftok" in url


def test_token_helpers(monkeypatch):
    monkeypatch.delenv("BOARD_IMAGE_TOKEN", raising=False)
    assert m._board_image_token() == ""
    monkeypatch.setenv("BOARD_IMAGE_TOKEN", "abc")
    assert m._board_image_token() == "abc"


# --------------------------------------------------------------------------- #
# Team-scoped renders (web54a team-channel posts)
# --------------------------------------------------------------------------- #
def test_page_url_carries_team_param(monkeypatch):
    monkeypatch.setenv("BOARD_IMAGE_TOKEN", "sekret")
    url = m.board_image_page_url(7)
    assert "&team=" not in url
    team_url = m.board_image_page_url(7, team_id=3)
    assert team_url.startswith(url)
    assert team_url.endswith("&team=3")


def test_dispatch_passes_team_to_bingo_signature(monkeypatch):
    seen = {}

    def fake_bingo(s, e, team_id=None):
        seen["team"] = team_id
        return {"kind": "bingo"}

    monkeypatch.setattr(m, "_bingo_signature", fake_bingo)
    m._collect_render_inputs(object(), _event(has_bingo=True), team_id=42)
    assert seen["team"] == 42
