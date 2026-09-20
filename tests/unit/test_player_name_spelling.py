"""The stored player name must keep the account's real in-game spelling.

WOM exposes two names: ``username``, its standardized key (lowercased and
'-'/'_'/space folded by ``standardizeUsername``), and ``displayName``, what the
account is actually called in game. ``check_user_by_username`` returned
``username`` -- contradicting its own docstring -- so every row first created by
submission intake stored an all-lowercase, separator-folded name ("deadcllck"
for DEADCLlCK, "94 fable 92" for 94 Fable 92).

Nothing could repair it either: all four writers gated the update on
``normalize_player_display_equivalence``, which lowercases and folds
separators, so the correct spelling compared equal to the stored one and was
discarded -- the plugin submitted "DEADCLlCK" 94 times and we dropped it 94
times (ticket #441, ~900 rows).

Both halves are guarded here: the spelling rule's behaviour, and an AST parse
of each writer that fails if a name refresh is gated on the folded comparison
again.
"""

import ast
from pathlib import Path

import pytest

from utils.rsn import better_spelling, normalize_player_display_equivalence

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestBetterSpelling:
    @pytest.mark.parametrize("stored,incoming,expected", [
        # The ticket's two accounts: case-only, and WOM is authoritative.
        ("deadcllck", "DEADCLlCK", "DEADCLlCK"),
        ("94 fable 92", "94 Fable 92", "94 Fable 92"),
        # WOM's displayName wins on separators too.
        ("zuk my feet", "Zuk-My-Feet", "Zuk-My-Feet"),
        ("ZE_ET", "ZE ET", "ZE ET"),
        # A real rename is applied, as before.
        ("oldname", "NewName", "NewName"),
        # ...but WOM's displayName is NOT always the game's spelling. When WOM
        # holds no display spelling it echoes the standardized key, so id
        # 2082247 reports displayName "r8d" while the plugin, reading the live
        # game name, sends "R8d". Adopting that echo overwrote a correct name
        # (caught in production 2026-09-20). A correction must never destroy
        # capitalisation we already hold.
        ("R8d", "r8d", None),
        ("IM Unleesh", "im unleesh", None),
        ("SClMMY", "sclmmy", None),
        # Separator changes between two cased spellings are still fine.
        ("ZE_ET", "ZE ET", "ZE ET"),
        # No churn when nothing changed.
        ("DEADCLlCK", "DEADCLlCK", None),
        ("DEADCLlCK", "", None),
        ("DEADCLlCK", None, None),
        (None, "Hilmor", "Hilmor"),
    ])
    def test_wom_display_name_is_authoritative(self, stored, incoming, expected):
        assert better_spelling(stored, incoming, authoritative=True) == expected

    @pytest.mark.parametrize("stored,incoming,expected", [
        # The plugin sends the game's spelling, so it is trusted for case...
        ("deadcllck", "DEADCLlCK", "DEADCLlCK"),
        ("im unleesh", "IM Unleesh", "IM Unleesh"),
        # ...but never for separators, or the hourly WOM sync and every
        # submission would rewrite the row in turn, forever.
        ("ZE_ET", "ZE ET", None),
        ("zuk my feet", "Zuk-My-Feet", None),
        # A submitted name that is a different name is not a spelling fix.
        ("oldname", "NewName", None),
    ])
    def test_submitted_name_fixes_case_only(self, stored, incoming, expected):
        assert better_spelling(stored, incoming, authoritative=False) == expected

    def test_a_correction_never_reduces_case_information(self):
        """The guard is directional: gaining case is a fix, losing it is not."""
        assert better_spelling("deadcllck", "DEADCLlCK", authoritative=True) == "DEADCLlCK"
        assert better_spelling("DEADCLlCK", "deadcllck", authoritative=True) is None
        # A name that genuinely holds no uppercase is not "losing" anything.
        assert better_spelling("r8d", "R8d", authoritative=True) == "R8d"

    def test_case_only_difference_is_not_an_identity_change(self):
        """The two questions must stay separate: a case-only difference is the
        same account (so no identity/ghost handling fires) but IS a spelling
        the row should adopt. Collapsing them is the original bug."""
        stored, incoming = "deadcllck", "DEADCLlCK"
        assert (normalize_player_display_equivalence(stored)
                == normalize_player_display_equivalence(incoming))
        assert better_spelling(stored, incoming, authoritative=True) == incoming


# (module path, function name) for every writer of players.player_name.
NAME_WRITERS = [
    ("data/submissions/common.py", "_apply_authoritative_wom_identity"),
    ("data/submissions/common.py", "ensure_player_and_auth"),
    ("data/submissions/common.py", "check_auth"),
    ("utils/wiseoldman.py", "fetch_group_members"),
    ("db/ops.py", "create_player"),
]


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


@pytest.mark.parametrize("rel_path,func_name", NAME_WRITERS)
def test_name_refresh_is_not_gated_on_the_folded_comparison(rel_path, func_name):
    """A writer must decide "rewrite the spelling?" with better_spelling, not
    with normalize_player_display_equivalence -- that helper answers "is this a
    different account?", and a case-only difference is the same account."""
    path = REPO_ROOT / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = _find_function(tree, func_name)
    assert func is not None, f"{func_name} not found in {rel_path}"

    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        # Looking for `<something>.player_name = <value>`
        targets = [t for t in node.targets
                   if isinstance(t, ast.Attribute) and t.attr == "player_name"]
        if not targets:
            continue
        # The assigned value must come from better_spelling (directly, or via a
        # local it returned). Anything guarded by the folded comparison is the
        # regression this test exists to catch.
        src = ast.unparse(func)
        assert "better_spelling" in src, (
            f"{rel_path}::{func_name} writes player_name without consulting "
            "better_spelling -- a case-only correction will be silently dropped "
            "(ticket #441)."
        )
        return

    pytest.fail(f"{rel_path}::{func_name} no longer writes player_name; "
                "update NAME_WRITERS if the writer moved.")


def test_wom_identity_lookup_returns_the_display_name():
    """check_user_by_username / check_user_by_id must hand back displayName.
    Returning WOM's lowercase ``username`` is what seeded ~900 folded rows."""
    src = (REPO_ROOT / "utils/wiseoldman.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for func_name in ("check_user_by_username", "check_user_by_id"):
        func = _find_function(tree, func_name)
        assert func is not None, f"{func_name} not found"
        body = ast.unparse(func)
        assert "_display_name_from_raw_player" in body, (
            f"{func_name} must resolve the name via _display_name_from_raw_player "
            "(displayName, falling back to username), not player.username."
        )
        assert "player.username, player.id" not in body, (
            f"{func_name} returns the raw lowercase WOM username again (ticket #441)."
        )
