"""Seasonal / Hardcore mirror tables must carry every column the shared
processors write on the main-game table.

The processors build rows on ``get_world_model(world_type, ...)``, so a
column added to the main model and not its mirrors crashes only for that
world. That happened with ``personal_best.precise_timing``: Hardcore Worlds
PBs dead-lettered on launch day (2026-11-04, web157a).
"""
import importlib

import pytest

_PAIRS = [
    ("PersonalBestEntry", ("SeasonalPersonalBestEntry", "HardcorePersonalBestEntry")),
]


@pytest.mark.parametrize("main,mirrors", _PAIRS)
def test_mirrors_have_the_main_tables_columns(main, mirrors):
    models = importlib.import_module("db.models")
    main_cols = {c.name for c in getattr(models, main).__table__.columns}
    for mirror in mirrors:
        cols = {c.name for c in getattr(models, mirror).__table__.columns}
        assert main_cols <= cols, f"{mirror} lacks {sorted(main_cols - cols)}"
