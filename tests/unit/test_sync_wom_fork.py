"""Guard logic for the wom.py fork auto-sync.

The script installs a new library version into prod and restarts four services
unattended, so the parts that decide *whether* to trust a commit are the ones
worth locking down: the pin rewrite (a bad substitution silently pins the wrong
sha) and the enum-shrink check (the only thing standing between a truncated
upstream sync and prod quietly losing metrics it used to recognise).
"""
import importlib.util
import os
import sys

import pytest

_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "sync_wom_fork.py")
)


@pytest.fixture()
def mod():
    """Load the script directly — conftest stubs `db`/`services`, and this
    module deliberately imports neither (stdlib only, so it still runs when the
    venv it manages is broken)."""
    spec = importlib.util.spec_from_file_location("_sync_wom_fork", _PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_sync_wom_fork"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("_sync_wom_fork", None)


SHA_A = "a" * 40
SHA_B = "b" * 40


def test_pin_regex_matches_the_real_requirements_line(mod):
    """Locks the regex against the actual file — a drift in the pin's format
    would otherwise make every run report 'pin line not found' forever."""
    with open(os.path.join(mod.DISC, "requirements.txt")) as f:
        content = f.read()
    match = mod.PIN_RE.search(content)
    assert match, "PIN_RE no longer matches requirements.txt"
    assert len(match.group(2)) == 40


def test_pin_rewrite_replaces_only_the_sha(mod):
    line = f"wom.py @ git+https://github.com/DropTracker-io/wom.py@{SHA_A}"
    out, count = mod.PIN_RE.subn(rf"\g<1>{SHA_B}", line)
    assert count == 1
    assert out == f"wom.py @ git+https://github.com/DropTracker-io/wom.py@{SHA_B}"


def test_pin_rewrite_leaves_unrelated_requirements_alone(mod):
    content = (
        "requests==2.32.4\n"
        f"wom.py @ git+https://github.com/DropTracker-io/wom.py@{SHA_A}\n"
        "pillow==12.2.0\n"
    )
    out, count = mod.PIN_RE.subn(rf"\g<1>{SHA_B}", content)
    assert count == 1
    assert "requests==2.32.4" in out and "pillow==12.2.0" in out


@pytest.mark.parametrize(
    "before, after, should_pass",
    [
        # Steady state: WOM published nothing new.
        ({"metrics": 114, "bosses": 71}, {"metrics": 114, "bosses": 71}, True),
        # The normal case this automation exists for: a boss was added.
        ({"metrics": 114, "bosses": 71}, {"metrics": 115, "bosses": 72}, True),
        # A truncated upstream sync — must be refused and rolled back.
        ({"metrics": 114, "bosses": 71}, {"metrics": 114, "bosses": 70}, False),
        ({"metrics": 114, "bosses": 71}, {"metrics": 9, "bosses": 71}, False),
    ],
)
def test_verify_refuses_a_shrinking_enum(mod, monkeypatch, before, after, should_pass):
    monkeypatch.setattr(mod, "enum_fingerprint", lambda: after)
    # Tests are stubbed out here; this case is specifically about the shrink gate.
    monkeypatch.setattr(mod, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    ok, detail = mod.verify(before)
    assert ok is should_pass
    if not should_pass:
        assert "shrank" in detail


def test_verify_refuses_a_package_that_will_not_import(mod, monkeypatch):
    """enum_fingerprint returns None when the import itself blows up — that must
    never be read as 'no change, carry on'."""
    monkeypatch.setattr(mod, "enum_fingerprint", lambda: None)
    ok, detail = mod.verify({"metrics": 114})
    assert ok is False
    assert "import" in detail


def test_verify_refuses_when_the_wom_unit_tests_fail(mod, monkeypatch):
    monkeypatch.setattr(mod, "enum_fingerprint", lambda: {"metrics": 999})
    monkeypatch.setattr(
        mod, "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "1 failed", "stderr": ""})(),
    )
    ok, detail = mod.verify({"metrics": 114})
    assert ok is False
    assert "unit tests failed" in detail


def test_install_rejects_a_sha_that_did_not_actually_land(mod, monkeypatch):
    """The whole reason this script exists: pip reports success while silently
    skipping the install, because the fork's version string never changes. A
    green pip exit is not evidence — direct_url.json is."""
    monkeypatch.setattr(
        mod, "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(mod, "installed_sha", lambda: SHA_A)  # stale: pip no-opped
    assert mod.install(SHA_B) is False
    monkeypatch.setattr(mod, "installed_sha", lambda: SHA_B)
    assert mod.install(SHA_B) is True


# --- sync_upstream: the job adds WOM's new metrics to the fork itself --------
# (The fork's GitHub schedule disabled itself after 60 quiet days on
# 2026-09-28, so The Fractured Archive never reached the library.)

class _Runs:
    """Scripted `run`: answers by matching a substring of the command."""

    def __init__(self, answers):
        self.answers = answers  # [(needle, returncode, stdout)]
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        rc, out = 0, ""
        for needle, code, text in self.answers:
            if needle in joined:
                rc, out = code, text
                break
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()

    def ran(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]


@pytest.fixture()
def clone(mod, tmp_path, monkeypatch):
    path = tmp_path / "wom-fork"
    (path / ".git").mkdir(parents=True)
    monkeypatch.setattr(mod, "FORK_CLONE", str(path))
    return path


NEW = "New metrics: Boss: TheFracturedArchive='the_fractured_archive'"


def test_upstream_nothing_new(mod, clone, monkeypatch):
    runs = _Runs([("sync_metrics.py", 0, "")])
    monkeypatch.setattr(mod, "run", runs)
    assert mod.sync_upstream(apply=True) is None
    assert not runs.ran(" push ")


def test_upstream_new_metric_is_tested_committed_and_pushed(mod, clone, monkeypatch):
    runs = _Runs([("sync_metrics.py", 10, NEW + "\n")])
    monkeypatch.setattr(mod, "run", runs)
    status = mod.sync_upstream(apply=True)
    assert status.startswith("pushed") and "the_fractured_archive" in status
    assert runs.ran("pytest tests")
    commit = runs.ran(" commit ")[0]
    assert commit[-2:] == ["--", "wom/enums.py"]  # only the enum
    assert runs.ran("push -q origin droptracker")


def test_upstream_dry_run_only_reports(mod, clone, monkeypatch):
    runs = _Runs([("sync_metrics.py", 10, NEW + "\n")])
    monkeypatch.setattr(mod, "run", runs)
    assert mod.sync_upstream(apply=False).startswith("DRY RUN")
    assert "--check" in runs.ran("sync_metrics.py")[0]
    assert not runs.ran(" commit ") and not runs.ran(" push ")


def test_upstream_failing_fork_tests_are_never_pushed(mod, clone, monkeypatch):
    runs = _Runs([("sync_metrics.py", 10, NEW + "\n"), ("-m pytest", 1, "1 failed")])
    monkeypatch.setattr(mod, "run", runs)
    assert mod.sync_upstream(apply=True).startswith("ERROR: fork tests failed")
    assert not runs.ran(" commit ") and not runs.ran(" push ")


def test_upstream_rejected_push_resets_the_clone(mod, clone, monkeypatch):
    runs = _Runs([("sync_metrics.py", 10, NEW + "\n"), (" push -q origin", 1, "")])
    monkeypatch.setattr(mod, "run", runs)
    assert mod.sync_upstream(apply=True).startswith("ERROR: push")
    assert runs.calls[-1][-1] == "origin/droptracker" and "reset" in runs.calls[-1]


def test_upstream_tool_failure_is_an_error_not_a_crash(mod, clone, monkeypatch):
    runs = _Runs([("sync_metrics.py", 1, "could not locate `declare const Boss`")])
    monkeypatch.setattr(mod, "run", runs)
    assert mod.sync_upstream(apply=True).startswith("ERROR: tools/sync_metrics.py exited 1")
