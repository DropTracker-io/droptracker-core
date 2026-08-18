"""The three intake transports must route identically.

The legacy Discord-webhook reader used to carry its own hand-maintained copy
of the dispatch table and fell behind: main-world ``death``, ``diary`` and
``quest`` submissions fell out of its if/elif chain and were discarded with no
row and no log line (all 29,850 player_deaths rows were used_api=1 as a
result). These tests fail if any caller grows a private routing branch again.
"""

import ast
import inspect
from pathlib import Path

import pytest

from data.submissions import dispatch


REPO_ROOT = Path(__file__).resolve().parents[2]


class TestCanonicalTable:
    def test_every_supported_type_names_a_real_processor(self):
        import data.submissions as submissions

        for submission_type, processor_name in dispatch._PROCESSORS.items():
            assert hasattr(submissions, processor_name), (
                f"{submission_type} routes to {processor_name}, which data.submissions "
                f"does not export"
            )

    def test_seasonal_types_are_a_subset_of_supported(self):
        assert dispatch.SEASONAL_TYPES <= dispatch.SUPPORTED_TYPES

    def test_seasonal_processors_all_accept_world_type(self):
        """experience/adventure_log take no world_type — calling them for a
        seasonal world raises TypeError rather than no-opping, so they must
        stay out of SEASONAL_TYPES."""
        import data.submissions as submissions

        for submission_type in dispatch.SEASONAL_TYPES:
            processor = getattr(submissions, dispatch._PROCESSORS[submission_type])
            params = inspect.signature(processor).parameters
            assert "world_type" in params, (
                f"{submission_type} is in SEASONAL_TYPES but its processor takes "
                f"no world_type argument"
            )

    def test_types_excluded_from_seasonal_take_no_world_type(self):
        import data.submissions as submissions

        for submission_type in dispatch.SUPPORTED_TYPES - dispatch.SEASONAL_TYPES:
            processor = getattr(submissions, dispatch._PROCESSORS[submission_type])
            params = inspect.signature(processor).parameters
            if "world_type" in params:
                # Fine to have one, but then it should be seasonally routed
                # unless it is a main-world-only concern (the clan relay).
                assert submission_type in {"clan_broadcast", "clan_chat"}, (
                    f"{submission_type} accepts world_type but is not in "
                    f"SEASONAL_TYPES — seasonal submissions of it are dropped"
                )

    def test_aliases_resolve_to_supported_types(self):
        for alias, canonical in dispatch.TYPE_ALIASES.items():
            assert canonical in dispatch.SUPPORTED_TYPES, (
                f"alias {alias!r} maps to {canonical!r}, which has no processor"
            )

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("other", "drop"),
            ("npc", "drop"),
            ("kill_time", "personal_best"),
            ("npc_kill", "personal_best"),
            ("experience_update", "experience"),
            ("xp_milestone", "experience"),
            ("level_up", "experience"),
            ("quest_completion", "quest"),
            ("player_death", "death"),
            ("PLAYER_DEATH", "death"),
            (" Death ", "death"),
            ("achievement_diary", "diary"),
            ("diary_completion", "diary"),
            ("pet", "pet"),
            (None, ""),
            ("", ""),
        ],
    )
    def test_normalization(self, raw, expected):
        assert dispatch.normalize_submission_type(raw) == expected

    def test_the_types_that_regressed_are_routable(self):
        """Regression guard for the reader's missing branches."""
        for submission_type in ("death", "diary", "quest"):
            assert dispatch.is_supported(submission_type)
            assert submission_type in dispatch.SEASONAL_TYPES


class TestDispatchCallsTheRightProcessor:
    """dispatch_submission() actually reaches each processor."""

    @pytest.fixture
    def spy(self, monkeypatch):
        import data.submissions as submissions

        calls = {}

        def make(name):
            async def _fake(data, external_session=None, world_type="main"):
                calls["name"] = name
                calls["world_type"] = world_type
                calls["session"] = external_session
                return f"{name}-response"
            return _fake

        for processor_name in dispatch._PROCESSORS.values():
            monkeypatch.setattr(submissions, processor_name, make(processor_name))
        return calls

    @pytest.mark.parametrize("submission_type", sorted(dispatch.SUPPORTED_TYPES))
    @pytest.mark.asyncio
    async def test_main_world_routing(self, spy, submission_type):
        result = await dispatch.dispatch_submission(submission_type, {}, "session")
        assert spy["name"] == dispatch._PROCESSORS[submission_type]
        assert spy["session"] == "session"
        # Main-world calls leave world_type at its default.
        assert spy["world_type"] == "main"
        assert result == f"{dispatch._PROCESSORS[submission_type]}-response"

    @pytest.mark.parametrize("submission_type", sorted(dispatch.SEASONAL_TYPES))
    @pytest.mark.asyncio
    async def test_seasonal_routing(self, spy, submission_type):
        await dispatch.dispatch_submission(
            submission_type, {}, "session", world_type="seasonal"
        )
        assert spy["name"] == dispatch._PROCESSORS[submission_type]
        assert spy["world_type"] == "seasonal"

    @pytest.mark.asyncio
    async def test_death_alias_routes_to_death_processor(self, spy):
        """The exact submission that used to vanish on the reader path."""
        await dispatch.dispatch_submission("player_death", {}, "session")
        assert spy["name"] == "death_processor"

    @pytest.mark.asyncio
    async def test_unsupported_type_returns_none(self, spy):
        assert await dispatch.dispatch_submission("nonsense", {}, "session") is None
        assert spy == {}

    @pytest.mark.asyncio
    async def test_seasonally_unsupported_type_is_not_called_with_world_type(self, spy):
        """experience_processor takes no world_type; routing it seasonally
        must return None rather than raise TypeError."""
        assert await dispatch.dispatch_submission(
            "experience", {}, "session", world_type="seasonal"
        ) is None
        assert spy == {}


class TestReaderEmbedTypeResolution:
    """The reader detects every type from the embed's own ``type`` field."""

    @staticmethod
    def _resolve(embed_data, title=None, field_names=(), field_values=()):
        return dispatch.resolve_submission_type(
            embed_data.get("type"), title, list(field_names), list(field_values)
        )

    @pytest.mark.parametrize("submission_type", sorted(dispatch.SUPPORTED_TYPES))
    def test_declared_type_field_is_honored(self, submission_type):
        assert self._resolve({"type": submission_type}) == submission_type

    @pytest.mark.parametrize(
        "alias,expected",
        [("player_death", "death"), ("achievement_diary", "diary"),
         ("quest_completion", "quest"), ("npc_kill", "personal_best"),
         ("xp_milestone", "experience"), ("other", "drop")],
    )
    def test_aliases_in_type_field(self, alias, expected):
        assert self._resolve({"type": alias}) == expected

    def test_legacy_embeds_without_type_field_still_detected(self):
        assert self._resolve({}, field_values=["collection_log"]) == "collection_log"
        assert self._resolve({}, title="Player received some drops") == "drop"
        assert self._resolve(
            {}, field_names=["pet_name"], field_values=["pet"]
        ) == "pet"

    def test_legacy_death_and_diary_now_detected(self):
        """Neither had a branch at all before — they returned None and the
        embed was discarded."""
        assert self._resolve({}, field_values=["player_death"]) == "death"
        assert self._resolve({}, field_values=["achievement_diary"]) == "diary"

    def test_unrecognized_embed_returns_none(self):
        assert self._resolve({}, field_values=["something_else"]) is None


def _called_processor_names(path: Path, exclude_functions=()):
    """Every ``*_processor(...)`` called directly in a module."""
    tree = ast.parse(path.read_text())
    excluded_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in exclude_functions:
            excluded_nodes.update(ast.walk(node))

    called = set()
    for node in ast.walk(tree):
        if node in excluded_nodes or not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name and name.endswith("_processor"):
            called.add(name)
    return called


class TestNoPrivateDispatchTables:
    """Transports must call dispatch_submission(), not processors directly.

    A direct processor call in a transport is how the tables drifted the first
    time: each new type had to be remembered in three places, and eventually
    was not.
    """

    def test_webhook_reader_calls_no_processor_directly(self):
        called = _called_processor_names(REPO_ROOT / "bots" / "webhook_bot.py")
        assert called == set(), (
            f"bots/webhook_bot.py calls {sorted(called)} directly instead of routing "
            f"through data.submissions.dispatch — that is exactly how death/diary/quest "
            f"went missing on this path"
        )

    def test_queue_consumer_calls_no_processor_directly(self):
        called = _called_processor_names(REPO_ROOT / "workers" / "webhook_consumer.py")
        assert called == set(), (
            f"workers/webhook_consumer.py calls {sorted(called)} directly instead of "
            f"routing through data.submissions.dispatch"
        )

    def test_api_intake_calls_no_processor_directly(self):
        """The /manual-submit route is excluded: it is a different contract
        (per-type field validation for web submissions, governed by
        manual_policy) and deliberately supports fewer types."""
        called = _called_processor_names(
            REPO_ROOT / "api" / "routes" / "webhook.py",
            exclude_functions=("manual_submit", "_process_manual_submission"),
        )
        assert called == set(), (
            f"api/routes/webhook.py calls {sorted(called)} directly outside the manual "
            f"submission route instead of routing through data.submissions.dispatch"
        )
