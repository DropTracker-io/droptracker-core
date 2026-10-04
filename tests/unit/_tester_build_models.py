"""The real tester-build models, on the SQLite harness.

``db.models`` is stubbed in the unit suite, so a test cannot import
db/models/tester_builds.py the ordinary way. This loads that file with the
harness's declarative Base (tests/unit/_tester_db.py) standing in for
``db.models.base.Base``, so tests run against the columns the application
really declares, not a hand-kept copy of them.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

from tests.unit import _tester_db as tdb

_PACKAGE = "_tester_build_models_pkg"


def _load():
    package = types.ModuleType(_PACKAGE)
    package.__path__ = []  # a package, so the model file's ``from .base`` resolves
    base = types.ModuleType(f"{_PACKAGE}.base")
    base.Base = tdb.Base
    sys.modules[_PACKAGE] = package
    sys.modules[f"{_PACKAGE}.base"] = base

    path = os.path.join(tdb.REPO_ROOT, "db", "models", "tester_builds.py")
    spec = importlib.util.spec_from_file_location(f"{_PACKAGE}.tester_builds", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_models = _load()

PluginTestDownload = _models.PluginTestDownload
PlayerPluginVersion = _models.PlayerPluginVersion
