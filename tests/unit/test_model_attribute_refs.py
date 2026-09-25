"""Guard against referencing a model attribute that doesn't exist.

`POST /events/{id}/award` checked the roster with
`s.query(EventTeamMember.id)`. `EventTeamMember`'s primary key is
`(team_id, player_id)`, and it has no `id`, so every manual award that
named a player 500'd with AttributeError (2026-09-24). The web52a pot read
died the same way on its first deploy (`func.count(EventTeamMember.id)`).

A runtime unit test cannot catch this: conftest stubs `db` with a MagicMock,
on which every attribute exists. Like test_submission_model_kwargs.py, this
reads both sides from source with `ast`: each model's class-level names, and
every `Model.attr` in runtime code where `Model` was imported from `db`.
"""

import ast
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "db" / "models"
# Runtime packages. `web/` (the legacy Jinja2 site, not for new work) is left
# out on purpose.
SCANNED_DIRS = ("api", "bots", "commands", "data", "data_api", "db", "games",
                "lootboard", "monitor", "osrs_api", "services", "utils",
                "web_api", "workers")
# Modules a model name is imported from (`api.core` re-exports db's models).
MODEL_MODULES = ("db", "api.core")
# Names every declarative class has without declaring them.
IMPLICIT_ATTRS = {"__table__", "__tablename__", "__table_args__", "__mapper__",
                  "__mapper_args__", "__name__", "__dict__", "__doc__",
                  "__module__", "__init__", "__class__", "metadata",
                  "registry", "query"}

# References known to be broken, each awaiting its own fix. Delete the entry
# with the fix: test_known_entries_are_still_broken fails once it is stale.
KNOWN_MISSING = {
    # cleanup_tracking_dicts filters on a column that doesn't exist (it's
    # `date_added`), so its 30-day purge has never run. Correcting the name
    # would start a bulk delete of `notified` rows — an owner decision.
    ("services/notification_service.py", "NotifiedSubmission", "created_at"),
}


def _base_name(node: ast.expr):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _model_attrs() -> dict[str, set[str]]:
    """Map every declarative model to the attribute names it defines.

    Columns, relationships, methods, properties and nested classes, plus its
    bases' names and any ``backref=`` name (those are added to the other
    class at mapping time, so any model may carry one)."""
    raw: dict[str, set[str]] = {}
    bases: dict[str, list[str]] = {}
    backrefs: set[str] = set()
    for path in MODELS_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg != "backref":
                        continue
                    value = kw.value
                    if isinstance(value, ast.Call) and value.args:
                        value = value.args[0]  # backref("name", ...)
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        backrefs.add(value.value)
            if not isinstance(node, ast.ClassDef) or not node.bases:
                continue
            names = set()
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    names |= {t.id for t in stmt.targets if isinstance(t, ast.Name)}
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names.add(stmt.target.id)
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(stmt.name)
            raw[node.name] = names
            bases[node.name] = [b for b in map(_base_name, node.bases) if b]

    def is_model(name: str, seen: frozenset = frozenset()) -> bool:
        if name == "Base":
            return True
        if name in seen or name not in bases:
            return False
        return any(is_model(b, seen | {name}) for b in bases[name])

    def resolve(name: str, seen: frozenset = frozenset()):
        """Names on ``name`` and its bases, or None when a base lives outside
        db/models (nothing to check it against)."""
        names = set(raw[name])
        for base in bases[name]:
            if base == "Base" or base in seen:
                continue
            if base not in raw:
                return None
            inherited = resolve(base, seen | {name})
            if inherited is None:
                return None
            names |= inherited
        return names

    models = {}
    for name in raw:
        if is_model(name):
            names = resolve(name)
            if names is not None:
                models[name] = names | backrefs | IMPLICIT_ATTRS
    return models


MODEL_ATTRS = _model_attrs()


def _model_refs(tree: ast.AST, rel: str) -> list[tuple]:
    """``(path, line, Model, attr)`` for every attribute read off a model
    imported from ``db`` / ``api.core``."""
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
                node.module in MODEL_MODULES or node.module.startswith("db.")):
            for alias in node.names:
                if alias.name in MODEL_ATTRS:
                    imported[alias.asname or alias.name] = alias.name
    if not imported:
        return []
    return [(rel, node.lineno, imported[node.value.id], node.attr)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in imported]


def _missing(refs: list[tuple]) -> list[tuple]:
    return [ref for ref in refs if ref[3] not in MODEL_ATTRS[ref[2]]]


def _runtime_refs() -> list[tuple]:
    refs = []
    for top in SCANNED_DIRS:
        for path in sorted((REPO_ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            with warnings.catch_warnings():
                # Invalid escape sequences elsewhere aren't this test's business.
                warnings.simplefilter("ignore")
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            refs += _model_refs(tree, path.relative_to(REPO_ROOT).as_posix())
    return refs


REFS = _runtime_refs()
MISSING = _missing(REFS)


def test_models_were_discovered():
    """A parsing regression must fail loudly, not silently pass every check."""
    assert "player_id" in MODEL_ATTRS["EventTeamMember"]
    assert "id" not in MODEL_ATTRS["EventTeamMember"]
    assert "id" in MODEL_ATTRS["EventCompletion"]
    assert len(REFS) > 1000, "the scan found almost no model references"


def test_the_scan_catches_the_award_bug():
    tree = ast.parse("from db import EventTeamMember as M\nM.player_id\nM.id\n")
    assert _missing(_model_refs(tree, "x.py")) == [("x.py", 3, "EventTeamMember", "id")]


def test_model_attributes_exist():
    """Every `Model.attr` in runtime code must be something the model defines.

    SQLAlchemy raises AttributeError at the reference, which kills the request
    or the loop iteration it sits in."""
    unknown = [ref for ref in MISSING if (ref[0], ref[2], ref[3]) not in KNOWN_MISSING]
    assert not unknown, "\n".join(
        f"{path}:{line}: {model}.{attr} — {model} has no such attribute"
        for path, line, model, attr in unknown)


def test_known_entries_are_still_broken():
    live = {(path, model, attr) for path, _line, model, attr in MISSING}
    stale = KNOWN_MISSING - live
    assert not stale, f"fixed — delete from KNOWN_MISSING: {sorted(stale)}"
