"""Guard against passing a non-existent field to a submission model.

`data/submissions/diary.py` passed `plugin_version=` to `DiaryCompletionEntry`,
which has no such column. SQLAlchemy's declarative constructor raises
`TypeError: 'plugin_version' is an invalid keyword argument` — so every
achievement-diary submission died before the row was stored or the
notification queued. It went unnoticed from 2026-07-12 to 2026-09-01
(`diary_completions` held 0 rows the whole time).

A runtime construction test cannot catch this: conftest stubs `db` with a
MagicMock, so `DiaryCompletionEntry(anything=1)` succeeds under the stub.
This reads both sides from source with `ast` instead.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "db" / "models"
SUBMISSIONS_DIR = REPO_ROOT / "data" / "submissions"


def _class_attrs(node: ast.ClassDef) -> set[str]:
    """Names assigned at class level — Column(), relationship(), plain attrs."""
    attrs = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    attrs.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            attrs.add(stmt.target.id)
    return attrs


def _model_fields() -> dict[str, set[str]]:
    """Map every declarative model class name to its settable field names.

    Bases are merged in so a model inheriting columns from a mixin still
    reports them.
    """
    raw: dict[str, set[str]] = {}
    bases: dict[str, list[str]] = {}
    for path in MODELS_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = [b.id for b in node.bases if isinstance(b, ast.Name)]
            # Only declarative models (directly or transitively off Base).
            if not base_names:
                continue
            raw[node.name] = _class_attrs(node)
            bases[node.name] = base_names

    resolved: dict[str, set[str]] = {}

    def resolve(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
        if name in resolved:
            return resolved[name]
        if name in seen or name not in raw:
            return set()
        fields = set(raw[name])
        for base in bases.get(name, []):
            fields |= resolve(base, seen | {name})
        resolved[name] = fields
        return fields

    return {name: resolve(name) for name in raw}


MODEL_FIELDS = _model_fields()


def _model_constructor_calls():
    """Yield (file, line, model_name, kwargs) for each model built in a processor."""
    for path in sorted(SUBMISSIONS_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            name = node.func.id
            if name not in MODEL_FIELDS:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg is not None}
            # `**kwargs` splat — nothing statically checkable.
            if any(kw.arg is None for kw in node.keywords):
                continue
            yield path.relative_to(REPO_ROOT), node.lineno, name, kwargs


CALLS = list(_model_constructor_calls())


def test_models_were_discovered():
    """A parsing regression must fail loudly, not silently pass every check."""
    assert "DiaryCompletionEntry" in MODEL_FIELDS
    assert "diary_name" in MODEL_FIELDS["DiaryCompletionEntry"]
    assert "plugin_version" not in MODEL_FIELDS["DiaryCompletionEntry"]
    assert CALLS, "no model constructor calls found under data/submissions"


@pytest.mark.parametrize(
    "path,lineno,model,kwargs",
    CALLS,
    ids=[f"{p}:{n}:{m}" for p, n, m, _ in CALLS],
)
def test_submission_model_kwargs_exist(path, lineno, model, kwargs):
    """Every keyword handed to a model constructor must be a real field.

    SQLAlchemy raises TypeError on an unknown kwarg, killing the submission
    before it is persisted or queued.
    """
    unknown = sorted(kwargs - MODEL_FIELDS[model])
    assert not unknown, (
        f"{path}:{lineno} passes {unknown} to {model}(), which has no such "
        f"field — SQLAlchemy will raise TypeError and drop the submission."
    )
