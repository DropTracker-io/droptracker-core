"""scripts/refresh_game_cache.py — rebuild cache-derived data only when the
game cache changes, and never replace a good catalogue with a broken one."""
import json

import pytest

from scripts import refresh_game_cache as job


@pytest.fixture
def paths(tmp_path, monkeypatch):
    live = tmp_path / "item_catalogue.live.json"
    structure = tmp_path / "collection_log_structure.live.json"
    monkeypatch.setattr(job, "LIVE_CATALOGUE", str(live))
    monkeypatch.setattr(job, "TRACKED_CATALOGUE", str(tmp_path / "tracked.json"))
    monkeypatch.setattr(job, "LIVE_STRUCTURE", str(structure))
    return live, structure


def _fake_extract(monkeypatch, items):
    calls = []

    def run(cmd, cwd=None, stdout=None, check=False, env=None):
        calls.append((cmd, env.get("OSRS_CACHE_COMMIT") if env else None))
        if stdout is not None:
            stdout.write(json.dumps({"cache_commit": env["OSRS_CACHE_COMMIT"],
                                     "items": items}))

        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(job.subprocess, "run", run)
    return calls


def test_nothing_runs_when_already_built_from_the_head(paths, monkeypatch):
    live, structure = paths
    live.write_text(json.dumps({"cache_commit": "abc", "items": {"1": {"n": "A"}}}))
    structure.write_text(json.dumps({"cache_commit": "abc"}))
    monkeypatch.setattr(job, "mirror_head", lambda: ("abc", "d"))
    calls = _fake_extract(monkeypatch, {})
    assert job.main(["--apply"]) == 0
    assert calls == []


def test_a_new_build_regenerates_both_pinned_to_one_commit(paths, monkeypatch):
    live, _ = paths
    live.write_text(json.dumps({"cache_commit": "old", "items": {"1": {"n": "A"}}}))
    monkeypatch.setattr(job, "mirror_head", lambda: ("new", "d"))
    calls = _fake_extract(monkeypatch, {"1": {"n": "A"}, "2": {"n": "B"}})
    assert job.main(["--apply"]) == 0
    assert {commit for _, commit in calls} == {"new"}
    assert len(calls) == 2  # catalogue + collection log
    assert json.loads(live.read_text())["items"]["2"] == {"n": "B"}


def test_a_shrunken_catalogue_is_refused(paths, monkeypatch):
    live, structure = paths
    old = {str(i): {"n": f"Item {i}"} for i in range(100)}
    live.write_text(json.dumps({"cache_commit": "old", "items": old}))
    structure.write_text(json.dumps({"cache_commit": "new"}))
    monkeypatch.setattr(job, "mirror_head", lambda: ("new", "d"))
    _fake_extract(monkeypatch, {"1": {"n": "Item 1"}})
    assert job.main(["--apply"]) == 1
    assert json.loads(live.read_text())["items"] == old
    assert not (live.parent / (live.name + ".new")).exists()


def test_dry_run_writes_nothing(paths, monkeypatch):
    live, _ = paths
    live.write_text(json.dumps({"cache_commit": "old", "items": {"1": {"n": "A"}}}))
    monkeypatch.setattr(job, "mirror_head", lambda: ("new", "d"))
    calls = _fake_extract(monkeypatch, {"1": {"n": "A"}, "2": {"n": "B"}})
    assert job.main([]) == 0
    assert json.loads(live.read_text())["cache_commit"] == "old"
    clog_cmd = calls[1][0]
    assert "--apply" not in clog_cmd and clog_cmd[-1].endswith(".dryrun")


def test_unreachable_mirror_is_a_quiet_no_op(paths, monkeypatch):
    monkeypatch.setattr(job, "mirror_head", lambda: None)
    calls = _fake_extract(monkeypatch, {})
    assert job.main(["--apply"]) == 0 and calls == []
