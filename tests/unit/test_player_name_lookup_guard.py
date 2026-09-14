"""Functions that resolve a player from a TYPED name must not match it strictly.

OSRS treats '-', '_' and ' ' as one character, and ``players.player_name``
usually holds WOM's folded spelling ("1 19", "tzuk kal lag") while people type
the game's ("1-19", "Tzuk-Kal-Lag"). ``utf8mb4_general_ci`` does not bridge
them and ``ilike`` reads '_' as a wildcard, so each of these surfaces silently
missed hyphenated players until 2026-09-13 (ticket #434): /claim-rsn, site
search, the points commands, event bulk add, Modify Splits, /hideme, /submit,
/player_search, /player, /manual-submit and a notification fallback.

They now go through ``utils.rsn.find_player_by_rsn`` /
``pick_player_by_rsn`` / ``player_name_search_expr``, ``db.ops.
resolve_player_for_display`` or ``drop._resolve_group_split_members``. This
test parses each one and fails if a strict comparison on ``player_name`` comes
back. Behaviour is covered in test_rsn_lookup.py and
test_split_participant_name_lookup.py.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

GUARDED = [
    ("commands/group_admin.py", "add_group_points_cmd"),
    ("commands/group_admin.py", "remove_group_points_cmd"),
    ("commands/group_admin.py", "group_points_audit_cmd"),
    ("commands/group_admin.py", "_resolve_target"),
    ("commands/user.py", "hideme_cmd"),
    ("commands/submissions.py", "_resolve_player"),
    ("services/entry_modifier.py", "_process_modify_splits"),
    ("api/routes/players.py", "player_search"),
    ("api/routes/players.py", "get_player"),
    ("api/routes/webhook.py", "_process_manual_submission"),
    ("web_api/routes/search.py", "_search_players"),
    ("web_api/routes/admin.py", "admin_lookup"),
    ("web_api/routes/events.py", "admin_add_members_bulk"),
    ("services/notification_service.py", "send_drop_notification_with_session"),
    # In-memory filters and autocompletes over names already loaded. (The web
    # members filter and _is_same_member_as_target compare through local
    # variables this parse cannot follow, so they are not listed.)
    ("commands/group_admin.py", "add_group_points_autocomplete"),
    ("commands/group_admin.py", "remove_group_points_autocomplete"),
    ("commands/group_admin.py", "group_points_audit_autocomplete"),
    ("web_api/routes/events.py", "get_completion_history"),
]

MATCH_METHODS = {"ilike", "like", "contains", "startswith", "endswith", "in_"}


def _is_player_name(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "player_name"


def _derives_from_player_name(node) -> bool:
    """``x.player_name``, ``row["player_name"]``, or a method chain on either
    (``(p.player_name or "").strip().lower()``). A value passed through a
    function -- ``normalize_player_display_equivalence(p.player_name)`` -- is
    not a strict match and does not count."""
    if _is_player_name(node):
        return True
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "player_name"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return _derives_from_player_name(node.func.value)
    if isinstance(node, ast.BoolOp):
        return any(_derives_from_player_name(v) for v in node.values)
    return False


def strict_name_matches(source: str, function_name: str) -> list[str]:
    """Line-tagged strict ``player_name`` matches inside ``function_name``."""
    tree = ast.parse(source)
    functions = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function_name
    ]
    assert functions, f"{function_name} not found — update GUARDED if it was renamed"
    found = []
    for fn in functions:
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare) and any(
                _derives_from_player_name(side) for side in [node.left, *node.comparators]
            ):
                found.append(f"line {node.lineno}: comparison on player_name")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in MATCH_METHODS and _is_player_name(node.func.value):
                    found.append(f"line {node.lineno}: player_name.{node.func.attr}()")
                elif node.func.attr == "filter_by" and any(
                    kw.arg == "player_name" for kw in node.keywords
                ):
                    found.append(f"line {node.lineno}: filter_by(player_name=...)")
                elif node.func.attr == "lower" and any(_is_player_name(a) for a in node.args):
                    found.append(f"line {node.lineno}: lower(player_name)")
    return found


@pytest.mark.parametrize("relpath, function_name", GUARDED)
def test_typed_name_lookup_is_not_strict(relpath, function_name):
    source = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert strict_name_matches(source, function_name) == [], (
        f"{relpath}::{function_name} matches a typed name strictly; resolve it with "
        "utils.rsn.find_player_by_rsn / pick_player_by_rsn instead"
    )


def test_guard_catches_the_patterns_it_exists_for():
    source = '''
def lookup(session, Player, name, rows):
    a = session.query(Player).filter(Player.player_name == name).first()
    b = session.query(Player).filter(Player.player_name.ilike(name)).first()
    c = session.query(Player).filter_by(player_name=name).first()
    d = session.query(Player).filter(func.lower(Player.player_name).in_(rows)).all()
    e = [p for p in rows if (p.player_name or "").strip().lower() == name.lower()]
    f = [r for r in rows if name.lower() in r["player_name"].lower()]
    ok = [p for p in rows if normalize_player_display_equivalence(p.player_name) == name]
'''
    found = strict_name_matches(source, "lookup")
    assert len(found) == 6, found
