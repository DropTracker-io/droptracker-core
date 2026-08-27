"""Combat achievement task-name canonicalization (``utils/ca_tasks.py``).

The regression these guard is 2026-08-26: Jagex began wrapping the task name in
``@ach_comp@`` and 2,921 completions landed under names no previous row had, so
each was treated as a first completion — duplicate row, duplicate Discord
notification, raw token in the message.
"""
import asyncio
import json

import pytest

from utils import ca_tasks
from utils.ca_tasks import (
    ResolvedTask,
    clean_task_name,
    resolve_task_name,
    task_key,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    ca_tasks.invalidate_caches()
    yield
    ca_tasks.invalidate_caches()


class _FakeRow:
    def __init__(self, payload):
        self.key = ca_tasks.MANIFEST_SECTION
        self.payload = payload


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _FakeSession:
    """Stands in for the ORM session; returns one manifest row."""

    def __init__(self, names):
        payload = json.dumps({"tasks": [{"name": n} for n in names]})
        self._row = _FakeRow(payload)

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._row)


class _BrokenSession:
    def query(self, *_args, **_kwargs):
        raise RuntimeError("database is down")


def _resolve(raw, **kwargs):
    return asyncio.run(resolve_task_name(raw, **kwargs))


# ── cleaning ────────────────────────────────────────────────────────────────

def test_strips_the_ach_comp_token():
    assert clean_task_name("@ach_comp@Smite Fight") == "Smite Fight"


def test_strips_legacy_colour_codes_and_angle_markup():
    assert clean_task_name("@red@Juggling Act") == "Juggling Act"
    assert clean_task_name("<col=ff0000>Dancing Queen</col>") == "Dancing Queen"


def test_strips_points_suffix_and_trailing_period():
    assert clean_task_name("@ach_comp@Whack-a-Mole (3 points).") == "Whack-a-Mole"


def test_is_idempotent_and_leaves_clean_names_alone():
    for name in ("Smite Fight", "You're a wizard", "Inferno Speed-Chaser"):
        assert clean_task_name(name) == name
        assert clean_task_name(clean_task_name(name)) == name


@pytest.mark.parametrize("name", [
    "Back in My Day...",
    "From Dusk...",
    "Shadows Move...",
    "Maybe I'm the boss.",
])
def test_keeps_the_trailing_period_of_tasks_that_really_end_in_one(name):
    """These four are real task names and 1,043 stored rows carry them.

    A general trailing-period strip looks harmless and would fork every one of
    them off its own history — the same failure this module exists to prevent.
    """
    assert clean_task_name(name) == name
    assert clean_task_name("@ach_comp@" + name) == name
    assert clean_task_name(f"@ach_comp@{name} (5 points).") == name


def test_tolerates_non_string_input():
    assert clean_task_name(None) == ""
    assert clean_task_name("   ") == ""


def test_a_name_that_is_only_markup_resolves_to_nothing():
    """``ca_processor`` aborts on an empty name rather than storing the raw
    input — falling back to the raw string would put the markup straight back
    into the row and the Discord embed."""
    result = _resolve("@ach_comp@")
    assert result.name == ""
    assert not result.verified


def test_does_not_eat_a_name_between_stray_ats():
    # The bounded token length is what stops a generic @...@ pattern from
    # swallowing real text; nothing here should vanish.
    assert clean_task_name("a @ b @ c") == "a @ b @ c"


# ── matching ────────────────────────────────────────────────────────────────

def test_task_key_folds_punctuation_and_case_only():
    assert task_key("Inferno Speed-Chaser") == task_key("inferno speed chaser")
    # Two genuinely different tasks must never collide.
    assert task_key("Royal Titan Adept") != task_key("Royal Titan Champion")
    # A plural is a different name, not a spelling variant.
    assert task_key("Perfect Royal Titan") != task_key("Perfect Royal Titans")


def test_catalog_match_returns_the_catalogued_spelling():
    session = _FakeSession(["Smite Fight", "Perfect Royal Titans"])
    result = _resolve("@ach_comp@smite  fight", session=session)
    assert result == ResolvedTask(
        name="Smite Fight", source="catalog", raw="@ach_comp@smite  fight"
    )
    assert result.verified
    assert result.cleaned


def test_clean_name_already_in_catalog_is_not_reported_as_cleaned():
    session = _FakeSession(["Smite Fight"])
    result = _resolve("Smite Fight", session=session)
    assert result.source == "catalog"
    assert not result.cleaned


# ── the wiki fallback ───────────────────────────────────────────────────────

def test_falls_back_to_the_wiki_when_the_catalog_lags(monkeypatch):
    # The exact 2026-08-26 shape: new content the cache-derived registry has
    # not been rebuilt for. It must resolve, not be rejected.
    session = _FakeSession(["Smite Fight"])
    monkeypatch.setattr(
        ca_tasks, "_fetch_wiki_index", _async_index({"Mad Angel Adept"})
    )
    result = _resolve("@ach_comp@Mad Angel Adept", session=session)
    assert result.name == "Mad Angel Adept"
    assert result.source == "wiki"
    assert result.verified


def test_unknown_name_is_still_returned_cleaned_and_flagged(monkeypatch):
    session = _FakeSession(["Smite Fight"])
    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _async_index(set()))
    result = _resolve("@ach_comp@Definitely Not A Task", session=session)
    assert result.name == "Definitely Not A Task"
    assert result.source == "unverified"
    assert not result.verified


def test_catalog_hit_never_calls_the_wiki(monkeypatch):
    session = _FakeSession(["Smite Fight"])

    async def _explode(_cache):
        raise AssertionError("wiki must not be consulted on a catalog hit")

    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _explode)
    assert _resolve("@ach_comp@Smite Fight", session=session).source == "catalog"


# ── failing open ────────────────────────────────────────────────────────────

def test_a_broken_database_still_yields_a_cleaned_name(monkeypatch):
    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _async_index(set()))
    result = _resolve("@ach_comp@Smite Fight", session=_BrokenSession())
    assert result.name == "Smite Fight"
    assert result.source == "unverified"


def test_a_broken_wiki_still_yields_a_cleaned_name(monkeypatch):
    async def _explode(_cache):
        raise RuntimeError("403 from the wiki")

    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _explode)
    result = _resolve("@ach_comp@Smite Fight", session=_FakeSession(["Other"]))
    assert result.name == "Smite Fight"
    assert result.source == "unverified"


def test_empty_catalog_is_never_cached(monkeypatch):
    """The registry row is written by a script that may run after boot.

    Caching "no tasks" would pin every submission to unverified until the
    process restarted — the trap already documented for the collection log.
    """
    empty = _FakeSession([])
    assert ca_tasks.catalog_index(empty) == {}
    populated = _FakeSession(["Smite Fight"])
    assert ca_tasks.catalog_index(populated) != {}


def test_resolution_works_without_a_session_or_cache(monkeypatch):
    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _async_index(set()))
    assert _resolve("@ach_comp@Smite Fight").name == "Smite Fight"


# ── fetch throttling ────────────────────────────────────────────────────────

def test_a_failing_wiki_is_not_refetched_per_submission(monkeypatch):
    """A wiki outage must not become sustained load against a host already
    refusing us — the failure mode of the 2026-08 UA blocklisting."""
    calls = []

    async def _fail(_cache):
        calls.append(1)
        return {}

    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _fail)
    session = _FakeSession(["Smite Fight"])
    for _ in range(5):
        assert _resolve("Not A Task", session=session).source == "unverified"
    assert len(calls) == 1


def test_a_run_of_unknown_names_triggers_one_fetch(monkeypatch):
    calls = []

    async def _count(_cache):
        calls.append(1)
        return {task_key("Smite Fight"): "Smite Fight"}

    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _count)
    session = _FakeSession(["Other Task"])
    for i in range(5):
        _resolve(f"Junk Name {i}", session=session)
    assert len(calls) == 1


def test_the_throttle_expires(monkeypatch):
    calls = []

    async def _count(_cache):
        calls.append(1)
        return {}

    monkeypatch.setattr(ca_tasks, "_fetch_wiki_index", _count)
    session = _FakeSession(["Other Task"])
    _resolve("Brand New Task", session=session)
    assert len(calls) == 1
    # Wind the clock past the floor; the next miss may fetch again, which is
    # how a task released after the last fetch is eventually recognized.
    ca_tasks._wiki_retry_after = 0.0
    _resolve("Brand New Task", session=session)
    assert len(calls) == 2


def _async_index(names):
    async def _fetch(_cache):
        return {task_key(n): n for n in names}

    return _fetch
