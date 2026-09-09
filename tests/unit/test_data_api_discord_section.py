"""The `discord` section: the /claim-rsn link read back out.

Two things are worth pinning independently of a database. The shaping rules —
a snowflake must stay a string, and "a users row exists" is not the same as
"someone claimed this" — and the registry properties that decide who receives
the data at all: it is priced, and `all` does not expand to it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sect = _load("_real_sections", "data_api/sections.py")


class _Session:
    """One canned result set, the shape the loader's query returns."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = 0

    def execute(self, _statement):
        self.queries += 1
        return list(self.rows)


class TestShape:
    def test_a_claimed_player_carries_the_id_as_a_string(self):
        # A snowflake is larger than a JSON number survives in JavaScript, so
        # it must not go out as an int even though it looks like one.
        out = sect._load_discord(_Session([(1593, 528746710982163011)]), [1593], {})
        assert out[1593] == {"discord_id": "528746710982163011", "claimed": True}
        assert isinstance(out[1593]["discord_id"], str)

    def test_an_unclaimed_player_is_null_not_missing(self):
        # The caller iterates the roster; a player with no claim still needs an
        # answer, or "not claimed" is indistinguishable from "not returned".
        out = sect._load_discord(_Session([(1699, None)]), [1699], {})
        assert out[1699] == {"discord_id": None, "claimed": False}

    def test_a_users_row_without_a_discord_id_is_not_a_claim(self):
        # The website's forum import creates users rows with no discord_id.
        # Reporting claimed=True for one would have the clan bot look up a
        # member that does not exist.
        out = sect._load_discord(_Session([(1190, ""), (1191, "   ")]), [1190, 1191], {})
        assert out[1190]["claimed"] is False
        assert out[1191] == {"discord_id": None, "claimed": False}

    def test_the_whole_page_is_one_query(self):
        session = _Session([(1, "10"), (2, "20"), (3, None)])
        out = sect._load_discord(session, [1, 2, 3], {})
        assert session.queries == 1
        assert len(out) == 3


class TestRegistry:
    def test_it_is_priced_not_free(self):
        # Free would mean unlimited polling of personal identifiers outside
        # the budget that governs every other read.
        assert sect.REGISTRY["discord"].cost >= 1

    def test_all_does_not_expand_to_it(self):
        # An integration already calling include=all must not silently begin
        # receiving Discord ids it never asked for.
        assert sect.REGISTRY["discord"].in_all is False
        assert "discord" not in sect.parse_include("all")

    def test_it_is_still_available_by_name(self):
        assert sect.parse_include("discord") == ["identity", "discord"]

    def test_it_is_not_folded_into_identity(self):
        # identity is the default section: folding the id in there would hand
        # it to every caller who asked for nothing.
        assert sect.REGISTRY["discord"].loader is not sect.REGISTRY["identity"].loader
        assert "discord" not in sect.DEFAULT_SECTIONS
