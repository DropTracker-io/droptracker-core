"""Tests for the procedural board-game generator's pure half
(``services.boardgame_generator``). Exercises the boardgen -> EventBoardTile
mapping without B2 or chromium: param clamping, the tile-track contract the
PUT /board endpoint enforces, and reproducibility.

The conftest stubs the ``services`` package, so — like test_boardgame_engine —
the real module loads by file path. We first register the real (stdlib-only)
``services.boardgen`` subpackage in sys.modules so the generator's
``from services.boardgen import ...`` resolves to the real engine, without
disturbing the stubbed ``services`` package other tests rely on.
"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _ensure_real_boardgen():
    if getattr(sys.modules.get("services.boardgen"), "Board", None) is not None:
        return
    pkg_dir = _ROOT / "services" / "boardgen"
    spec = importlib.util.spec_from_file_location(
        "services.boardgen", pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["services.boardgen"] = mod          # register before exec (relative imports)
    spec.loader.exec_module(mod)


def _load_generator():
    _ensure_real_boardgen()
    path = _ROOT / "services" / "boardgame_generator.py"
    spec = importlib.util.spec_from_file_location("_real_boardgame_generator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_real_boardgame_generator"] = mod   # @dataclass needs the module registered
    spec.loader.exec_module(mod)
    return mod


gen = _load_generator()
DIFFS = gen.EVENT_TASK_DIFFICULTIES


def test_normalize_clamps_and_defaults():
    p = gen.normalize_params(seed=9, regions=99, tiles=5000, style="bogus")
    assert p.seed == 9
    assert p.regions == 11          # clamped to MAX_REGIONS
    assert p.tiles == 400           # clamped to MAX_TILES
    assert p.style == "path"        # unknown style falls back
    d = gen.normalize_params()      # blank/None -> sensible defaults
    assert 2 <= d.regions <= 11 and 10 <= d.tiles <= 400 and d.style in ("path", "filled")
    assert d.title and d.subtitle


def test_seed_is_reproducible():
    a = gen.build_board_assets(gen.normalize_params(seed=42, regions=8, tiles=60))
    b = gen.build_board_assets(gen.normalize_params(seed=42, regions=8, tiles=60))
    assert a["tiles"] == b["tiles"]
    assert a["width"] == b["width"] and a["height"] == b["height"]


def test_tiles_honor_the_board_endpoint_contract():
    assets = gen.build_board_assets(
        gen.normalize_params(seed=9, regions=11, tiles=100, watermark="DropTracker.io"))
    tiles = assets["tiles"]
    # The movable-tile count is EXACT: the admin types 100, the board has 100.
    assert len(tiles) == 100
    assert assets["meta"]["path_tiles"] == 100

    # idx covers 0..N-1 exactly and in order (a contiguous, ordered track).
    assert [t["idx"] for t in tiles] == list(range(len(tiles)))

    # x/y are fractional positions strictly on the image.
    for t in tiles:
        assert 0.0 <= t["x"] <= 1.0
        assert 0.0 <= t["y"] <= 1.0

    # Ends are start/finish; the middle is all normal.
    assert tiles[0]["tile_kind"] == "start"
    assert tiles[-1]["tile_kind"] == "finish"
    assert all(t["tile_kind"] == "normal" for t in tiles[1:-1])

    # Difficulty cycles air -> water -> earth -> fire by index.
    for t in tiles:
        assert t["difficulty"] == DIFFS[t["idx"] % 4]

    # SVG rides along with sane dimensions for the background.
    assert assets["svg"].lstrip().startswith("<svg")
    assert assets["width"] > 0 and assets["height"] > 0
    assert assets["meta"]["skipped_regions"] == 0
