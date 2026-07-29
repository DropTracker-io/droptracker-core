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
