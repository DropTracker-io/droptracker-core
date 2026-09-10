"""The speck redraw (scripts/rerender_model_specks.py).

What matters is what it picks to redraw, and that it cannot run usefully
before the fixed renderer is live. A redraw that comes back as another speck
must stop the run, not go on through two thousand chromium screenshots.

The script imports only stdlib at module level; loading it by file path under
a throwaway name keeps the conftest's stubs in play.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
WIDTH, HEIGHT = 800, 1200


@pytest.fixture
def rerender():
    spec = importlib.util.spec_from_file_location(
        "_rerender_model_specks_under_test",
        REPO_ROOT / "scripts" / "rerender_model_specks.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def png(*boxes):
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    for left, top, right, bottom in boxes:
        img.paste(Image.new("RGBA", (right - left, bottom - top), (200, 150, 100, 255)),
                  (left, top))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# The shape the old pet placement drew, as measured on production renders: the
# player and the pet as two specks at mid-height, ~480 px apart.
SPECK = png((85, 576, 105, 623), (569, 610, 585, 623))
# A character drawn at the fixed camera's scale.
FIGURE = png((340, 300, 460, 930))


def test_measures_a_speck_a_figure_and_an_empty_frame(rerender):
    assert rerender.figure_fraction(SPECK) < rerender.SPECK_MAX_FIGURE
    assert rerender.figure_fraction(FIGURE) > 0.5
    assert rerender.figure_fraction(png()) == 0.0


@pytest.fixture
def world(rerender, monkeypatch):
    """A fake image tree: three players' model dirs and what their renders show."""
    renders = {
        (1, "aa"): SPECK,     # pet outfit: a speck, model stored -> redraw
        (1, "dd"): FIGURE,    # small file but a real figure -> leave alone
        (2, "ee"): SPECK,     # current outfit, no pet now -> redraw, first
    }
    dirs = [
        (1, {"aa.png": 9_000, "aa.glb": 50_000, "aa-pet.glb": 12_000,
             "bb.png": 9_000,                        # model pruned: cannot redraw
             "cc.png": 80_000, "cc.glb": 50_000,     # full-size render: not a speck
             "dd.png": 9_000, "dd.glb": 50_000,
             "aa-avatar.png": 15_000}),
        (2, {"ee.png": 9_000, "ee.glb": 50_000}),
    ]
    rendered, dropped = [], []
    fixed = {"renderer": True}

    async def fake_render(player_id, fingerprint, *, force=False):
        assert force, "a speck is only redrawn when the existing render is overridden"
        rendered.append((player_id, fingerprint))
        renders[(player_id, fingerprint)] = FIGURE if fixed["renderer"] else SPECK
        return f"https://cdn/{player_id}/{fingerprint}.png"

    monkeypatch.setattr(rerender, "_player_dirs", lambda: iter(dirs))
    monkeypatch.setattr(rerender, "_read_render", lambda p, f: renders.get((p, f)))
    monkeypatch.setattr(rerender, "_current_outfits", lambda: {(2, "ee")})
    monkeypatch.setattr(rerender, "_drop_avatar", lambda p, f: dropped.append((p, f)))
    monkeypatch.setattr(sys.modules["services.gear_image"], "render_gear_image", fake_render)
    return SimpleNamespace(rendered=rendered, dropped=dropped, fixed=fixed)


def run(rerender, **kw):
    args = SimpleNamespace(apply=False, limit=0, pause=0)
    for k, v in kw.items():
        setattr(args, k, v)
    return asyncio.run(rerender._run(args))


def test_dry_run_picks_confirmed_specks_with_a_model_current_outfits_first(
        rerender, world, capsys):
    assert run(rerender) == 0
    out = capsys.readouterr().out
    assert world.rendered == []
    lines = [line for line in out.splitlines() if "would redraw player" in line]
    assert lines == [
        "  would redraw player 2 outfit ee (current)",
        "  would redraw player 1 outfit aa with pet",
    ]
    assert "would redraw 2 specks; 1 of them current or pinned outfits, 1 with a pet" in out
    assert "1 small renders were real figures" in out


def test_apply_redraws_and_drops_the_crop_cut_from_the_old_render(rerender, world):
    assert run(rerender, apply=True) == 0
    assert world.rendered == [(2, "ee"), (1, "aa")]
    assert world.dropped == [(2, "ee"), (1, "aa")]


def test_stops_at_the_first_redraw_that_is_still_a_speck(rerender, world, capsys):
    world.fixed["renderer"] = False
    assert run(rerender, apply=True) == 1
    assert world.rendered == [(2, "ee")]
    assert world.dropped == []
    assert "still a speck" in capsys.readouterr().out


def test_limit_counts_confirmed_specks(rerender, world):
    assert run(rerender, apply=True, limit=1) == 0
    assert world.rendered == [(2, "ee")]
