"""bots/main.lootboard_updates: the "no saved message" path.

When a group had a lootboard channel but no saved message id, the updater
posted a placeholder and then read `message`, which only the other branches
assigned — an UnboundLocalError on every newly configured group's first cycle
(seen again on the dev instance's Bug Testers group). Checked on the source,
since the bot module cannot be imported in the unit suite.
"""
import ast
import os

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "bots", "main.py")


def _updater():
    tree = ast.parse(open(_MAIN, encoding="utf-8").read())
    return next(node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "lootboard_updates")


def test_message_is_reset_for_every_group():
    loop = next(node for node in ast.walk(_updater()) if isinstance(node, ast.For))
    body_try = next(node for node in loop.body if isinstance(node, ast.Try))
    first_assignments = [stmt for stmt in body_try.body[:6] if isinstance(stmt, ast.Assign)]
    assert any(isinstance(t, ast.Name) and t.id == "message" and
               isinstance(stmt.value, ast.Constant) and stmt.value.value is None
               for stmt in first_assignments for t in stmt.targets)


def test_the_placeholder_becomes_the_message():
    source = ast.get_source_segment(open(_MAIN, encoding="utf-8").read(), _updater())
    placeholder = source.index("This loot leaderboard is being initialized")
    following = source[placeholder:placeholder + 200]
    assert "message = new_board" in following
