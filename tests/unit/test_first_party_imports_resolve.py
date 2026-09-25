"""Every first-party import must name a module that exists.

``web_api/routes/event_discord.py`` imported ``services.event_leadership``
inside a handler, but that module lives at ``web_api/event_leadership.py``.
From web53a (2026-07-17) until 2026-09-25, every time a team captain opened
or saved their team's Discord notification settings, the route raised
ModuleNotFoundError and returned 500. Event admins return before that line,
so the people who build events never saw it.

No test could catch it. conftest stubs ``services`` with a MagicMock, so any
``services.<anything>`` import succeeds under pytest. Route modules
lazy-import inside handlers by design, so importing the app doesn't touch
the line either. This reads every import in the tree with ``ast``,
function-local ones included, and checks that its target file exists.
"""
import ast
import os
import warnings

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Hidden directories are skipped as well (.git, .claude worktrees, .trash-*).
_SKIP_DIRS = {"venv", "__pycache__", "node_modules", "logs", "static", "docs"}

# The migration env directory has the same name as the Alembic library.
# `import alembic.config` means the library, not a file under alembic/.
_NOT_FIRST_PARTY = {"alembic"}


def _skipped(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIRS


def _python_files():
    for root, dirs, files in os.walk(_REPO_ROOT):
        dirs[:] = [d for d in dirs if not _skipped(d)]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                yield os.path.relpath(path, _REPO_ROOT), path


def _first_party_roots() -> set[str]:
    roots = set()
    for entry in os.listdir(_REPO_ROOT):
        if _skipped(entry):
            continue
        if entry.endswith(".py"):
            roots.add(entry[:-3])
        elif os.path.isdir(os.path.join(_REPO_ROOT, entry)):
            roots.add(entry)
    return roots - _NOT_FIRST_PARTY


def _module_exists(module: str) -> bool:
    base = os.path.join(_REPO_ROOT, *module.split("."))
    return os.path.isfile(base + ".py") or os.path.isdir(base)


_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def _statements(nodes):
    """Every statement at any depth. Imports are statements, so skipping
    expression subtrees loses nothing and halves the time of ast.walk."""
    for node in nodes:
        yield node
        for field in _BLOCK_FIELDS:
            yield from _statements(getattr(node, field, None) or ())


def _imports(source: str, rel: str):
    """(line, module) for every import in ``source``, at any depth.

    A relative import is resolved against the package of the file it is in.
    ``from . import x`` is skipped, because ``x`` may be a submodule or just
    a name defined in the package.
    """
    with warnings.catch_warnings():
        # Other modules' invalid escape sequences are not this test's concern.
        warnings.simplefilter("ignore")
        tree = ast.parse(source, filename=rel)
    package = os.path.dirname(rel).replace(os.sep, ".").replace("/", ".")
    for node in _statements(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)]
                yield node.lineno, ".".join(parts + [node.module])
            else:
                yield node.lineno, node.module


def test_first_party_imports_name_real_modules():
    roots = _first_party_roots()
    missing = []
    for rel, path in _python_files():
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        for line, module in _imports(source, rel):
            if module.split(".")[0] in roots and not _module_exists(module):
                missing.append(f"{rel}:{line}: {module}")

    assert not missing, (
        "Import of a first-party module that does not exist. Under pytest "
        "the conftest stubs let services.*/db.* imports succeed anyway. In "
        "production this raises ModuleNotFoundError, which is a 500 inside "
        "a route:\n  " + "\n  ".join(missing)
    )


def test_a_function_local_import_of_a_missing_module_is_reported():
    """The route modules import inside handlers, which is where the
    event_discord bug was. The checker has to see imports there."""
    source = (
        "def handler():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
        "    from services.no_such_module import thing\n"
    )
    found = list(_imports(source, os.path.join("web_api", "routes", "example.py")))
    assert (6, "services.no_such_module") in found
    assert not _module_exists("services.no_such_module")
    assert _module_exists("web_api.event_leadership")


def test_relative_imports_resolve_against_their_package():
    found = list(_imports("from .utils import is_admin\n",
                          os.path.join("commands", "admin.py")))
    assert found == [(1, "commands.utils")]
    assert _module_exists("commands.utils")
