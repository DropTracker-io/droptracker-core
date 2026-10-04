"""utils/tester_builds.py: reading what the tester build job publishes.

The files belong to another job and can be missing, half-written or edited
by hand, so what these pin is mostly what does NOT happen:

* nothing raises, whatever is on disk;
* a zip is only ever named when its file name is a bare, safe one and the
  file is really there (the website puts that name in an X-Accel-Redirect
  header);
* the API shapes come out with the promised types even when the manifest's
  are wrong;
* a rewritten file is noticed, and an unchanged one is not parsed again.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from utils import tester_builds as tb

COMMIT = "2cd73be4f0a1b2c3d4e5f60718293a4b5c6d7e8f"
RELEASE_COMMIT = "577fa89000000000000000000000000000000000"
SHA256 = "ab" * 32


def _change(**over):
    change = {
        "commit": COMMIT, "short": "2cd73be", "version": "6.0.19",
        "title": "Clan chat sync works without the API",
        "subject": "v6.0.19: clan chat sync works without the API",
        "points": ["Syncs over the webhook", "No API key needed"],
        "author": "someone", "date": "2026-10-03T15:14:46+00:00", "pr": 59,
    }
    change.update(over)
    return change


def _manifest(build_id="6.0.19-2cd73be-rl1.13.1", built_at="2026-10-03T15:28:07Z", **over):
    manifest = {
        "schema": 1,
        "build_id": build_id,
        "file": f"droptracker-dev-{build_id}.zip",
        "size_bytes": 28563998,
        "sha256": SHA256,
        "built_at": built_at,
        "reason": "plugin",
        "repo_url": "https://github.com/joelhalen/droptracker-plugin",
        "plugin": {"version": "6.0.19", "commit": COMMIT, "short": "2cd73be",
                   "ref": "master", "committed_at": "2026-10-03T15:14:46+00:00"},
        "runelite_version": "1.13.1",
        "release": {"commit": RELEASE_COMMIT, "short": "577fa89",
                    "version": "6.0.12", "is_ancestor": True},
        "previous_build": None,
        "changes": [_change()],
        "since_release": [_change(), _change(commit=RELEASE_COMMIT, short="577fa89",
                                             version=None, title="Older", pr=None)],
        "smoke": {"script": "pass", "jar": "pass"},
    }
    manifest.update(over)
    return manifest


def _unix(*parts):
    return int(datetime(*parts, tzinfo=timezone.utc).timestamp())


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTER_BUILDS_DIR", str(tmp_path))
    (tmp_path / "builds").mkdir()
    return tmp_path


def _write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def _publish(root, manifest, zip_bytes=b"zip"):
    _write(root / "builds" / f"{manifest['build_id']}.json", manifest)
    if zip_bytes is not None:
        (root / "builds" / manifest["file"]).write_bytes(zip_bytes)
    return manifest


class TestBuildsDir:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("TESTER_BUILDS_DIR", raising=False)
        assert tb.builds_dir() == "/var/www/droptracker-tester-builds"

    def test_env_override_and_blank(self, monkeypatch):
        monkeypatch.setenv("TESTER_BUILDS_DIR", "/srv/builds")
        assert tb.builds_dir() == "/srv/builds"
        monkeypatch.setenv("TESTER_BUILDS_DIR", "  ")
        assert tb.builds_dir() == tb.DEFAULT_BUILDS_DIR


class TestLoadCurrent:
    def test_missing_directory_and_missing_file(self, root, monkeypatch):
        assert tb.load_current() is None
        assert tb.load_status() is None
        monkeypatch.setenv("TESTER_BUILDS_DIR", str(root / "nowhere"))
        assert tb.load_current() is None
        assert tb.list_builds() == []
        assert tb.zip_path(_manifest()) is None

    @pytest.mark.parametrize("content", [
        b"", b"{not json", b"[1, 2, 3]", b'"a string"', b"null",
        b"\xff\xfe not utf-8",
        json.dumps({"schema": 1}).encode(),                     # names no build
        json.dumps({"build_id": 6019}).encode(),                # not a string
        json.dumps({"build_id": "../../etc/passwd"}).encode(),  # not a plain id
        json.dumps({"build_id": "x" * 97}).encode(),            # longer than the column
        b"[" * 100_000,                                         # too deep for the parser
    ])
    def test_junk_is_no_build(self, root, content):
        (root / "current.json").write_bytes(content)
        assert tb.load_current() is None

    def test_reads_a_manifest(self, root):
        _write(root / "current.json", _manifest())
        assert tb.load_current()["build_id"] == "6.0.19-2cd73be-rl1.13.1"

    def test_explicit_root_wins_over_the_env(self, root, tmp_path_factory):
        other = tmp_path_factory.mktemp("other")
        _write(other / "current.json", _manifest("7.0.0-abc1234-rl1.14.0"))
        assert tb.load_current() is None
        assert tb.load_current(str(other))["build_id"] == "7.0.0-abc1234-rl1.14.0"

    def test_oversized_file_is_not_parsed(self, root, monkeypatch):
        monkeypatch.setattr(tb, "_MAX_JSON_BYTES", 64)
        _write(root / "current.json", _manifest())
        assert tb.load_current() is None

    def test_status(self, root):
        _write(root / "status.json", {"state": "ok"})
        assert tb.load_status() == {"state": "ok"}
        _write(root / "status.json", "nope")
        os.utime(root / "status.json", ns=(1, 1))
        assert tb.load_status() is None


class TestFileCache:
    def test_unchanged_file_is_parsed_once(self, root, monkeypatch):
        _write(root / "current.json", _manifest())
        calls = []
        real = tb._parse_file
        monkeypatch.setattr(tb, "_parse_file", lambda path: calls.append(path) or real(path))
        first = tb.load_current()
        assert tb.load_current() is first
        assert tb.load_current() is first
        assert len(calls) == 1

    def test_rewritten_file_is_picked_up(self, root):
        path = _write(root / "current.json", _manifest("6.0.19-2cd73be-rl1.13.1"))
        assert tb.load_current()["build_id"] == "6.0.19-2cd73be-rl1.13.1"
        before = os.stat(path)
        # Same length, written in place: only the modification time differs.
        _write(path, _manifest("6.0.20-2cd73be-rl1.13.1"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 5_000_000_000))
        assert tb.load_current()["build_id"] == "6.0.20-2cd73be-rl1.13.1"

    def test_replaced_file_is_picked_up_even_with_the_same_mtime_and_size(self, root):
        # The build job publishes by rename, which can land within the same
        # clock tick as the file it replaces.
        path = _write(root / "current.json", _manifest("6.0.19-2cd73be-rl1.13.1"))
        assert tb.load_current()["build_id"] == "6.0.19-2cd73be-rl1.13.1"
        before = os.stat(path)
        fresh = _write(root / "current.json.new", _manifest("6.0.20-2cd73be-rl1.13.1"))
        os.utime(fresh, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(fresh, path)
        assert os.stat(path).st_size == before.st_size
        assert tb.load_current()["build_id"] == "6.0.20-2cd73be-rl1.13.1"

    def test_removed_file_stops_being_served(self, root):
        path = _write(root / "current.json", _manifest())
        assert tb.load_current() is not None
        path.unlink()
        assert tb.load_current() is None


class TestZipPath:
    def test_safe_name_that_exists(self, root):
        manifest = _publish(root, _manifest())
        path = tb.zip_path(manifest)
        assert path == str(root / "builds" / manifest["file"])
        assert os.path.isabs(path)

    def test_zip_already_pruned(self, root):
        manifest = _publish(root, _manifest(), zip_bytes=None)
        assert tb.zip_path(manifest) is None

    def test_traversal_never_resolves_even_when_the_target_exists(self, root):
        (root / "x.zip").write_bytes(b"outside builds/")
        (root / "builds" / "a").mkdir()
        (root / "builds" / "a" / "b.zip").write_bytes(b"in a subdirectory")
        assert tb.zip_path(_manifest(file="../x.zip")) is None
        assert tb.zip_path(_manifest(file="a/b.zip")) is None
        assert tb.zip_path(_manifest(file=str(root / "x.zip"))) is None

    @pytest.mark.parametrize("name", [
        "../x.zip", "a/b.zip", "a\\b.zip", "/etc/x.zip", "x.zip\n", "x.zip\r\nX-Evil: 1",
        "x.exe", "x.zip.exe", ".zip", "", "has space.zip", "x.ZIP", "é.zip",
        "a" * 157 + ".zip", None, 7, ["x.zip"],
    ])
    def test_unsafe_names(self, name):
        assert tb.safe_zip_name(name) is None

    @pytest.mark.parametrize("name", [
        "droptracker-dev-6.0.19-2cd73be-rl1.13.1.zip", "a.zip", "A_b-1.2.zip",
        "a" * 156 + ".zip",
    ])
    def test_safe_names(self, name):
        assert tb.safe_zip_name(name) == name
        assert tb.SAFE_ZIP_RE.fullmatch(name)

    def test_a_directory_is_not_a_zip(self, root):
        (root / "builds" / "dir.zip").mkdir()
        assert tb.zip_path(_manifest(file="dir.zip")) is None

    def test_not_a_manifest(self, root):
        assert tb.zip_path(None) is None
        assert tb.zip_path("x.zip") is None
        assert tb.zip_path({}) is None


class TestListBuilds:
    def test_newest_built_first(self, root):
        _publish(root, _manifest("6.0.17-aaaaaaa-rl1.13.0", "2026-09-30T10:00:00Z"))
        _publish(root, _manifest("6.0.19-ccccccc-rl1.13.1", "2026-10-03T15:28:07Z"))
        _publish(root, _manifest("6.0.18-bbbbbbb-rl1.13.1", "2026-10-02T08:00:00+00:00"))
        _publish(root, _manifest("6.0.16-0000000-rl1.13.0", built_at=None))  # undated: last
        assert [m["build_id"] for m in tb.list_builds()] == [
            "6.0.19-ccccccc-rl1.13.1", "6.0.18-bbbbbbb-rl1.13.1",
            "6.0.17-aaaaaaa-rl1.13.0", "6.0.16-0000000-rl1.13.0",
        ]

    def test_limit(self, root):
        for day in range(1, 6):
            _publish(root, _manifest(f"6.0.{day}-abcdef0-rl1", f"2026-10-0{day}T00:00:00Z"))
        assert [m["build_id"] for m in tb.list_builds(limit=2)] == [
            "6.0.5-abcdef0-rl1", "6.0.4-abcdef0-rl1"]
        assert tb.list_builds(limit=0) == []
        assert tb.list_builds(limit="many") == []

    def test_skips_junk(self, root):
        good = _publish(root, _manifest())
        builds = root / "builds"
        _write(builds / "broken.json", "{nope")
        _write(builds / "a-list.json", [1, 2])
        _write(builds / "nameless.json", {"schema": 1})
        # A copy under another name would list the same build twice.
        _write(builds / "copy-of-build.json", good)
        _write(builds / "6.0.20.json.tmp", _manifest("6.0.20"))
        _write(builds / ".6.0.21.json", _manifest(".6.0.21"))
        _write(builds / "has space.json", _manifest("has space"))
        (builds / "a-directory.json").mkdir()
        assert [m["build_id"] for m in tb.list_builds()] == [good["build_id"]]

    def test_notices_new_changed_and_removed_builds(self, root):
        first = _publish(root, _manifest("6.0.18-bbbbbbb-rl1.13.1", "2026-10-02T08:00:00Z"))
        assert [m["build_id"] for m in tb.list_builds()] == [first["build_id"]]

        second = _publish(root, _manifest("6.0.19-ccccccc-rl1.13.1", "2026-10-03T08:00:00Z"))
        assert [m["build_id"] for m in tb.list_builds()] == [
            second["build_id"], first["build_id"]]

        # The older build is re-dated past the newer one.
        path = root / "builds" / f"{first['build_id']}.json"
        _write(path, dict(first, built_at="2026-10-04T08:00:00Z", reason="manual"))
        os.utime(path, ns=(1, 1))
        listed = tb.list_builds()
        assert [m["build_id"] for m in listed] == [first["build_id"], second["build_id"]]
        assert listed[0]["reason"] == "manual"

        path.unlink()
        assert [m["build_id"] for m in tb.list_builds()] == [second["build_id"]]

    def test_unchanged_manifests_are_not_parsed_again_to_order_them(self, root, monkeypatch):
        for day in range(1, 6):
            _publish(root, _manifest(f"6.0.{day}-abcdef0-rl1", f"2026-10-0{day}T00:00:00Z"))
        tb.list_builds(limit=1)
        calls = []
        real = tb._parse_file
        monkeypatch.setattr(tb, "_parse_file", lambda path: calls.append(path) or real(path))
        assert [m["build_id"] for m in tb.list_builds(limit=1)] == ["6.0.5-abcdef0-rl1"]
        # Only the one it returns is read; the other four are ordered from memory.
        assert len(calls) == 1


class TestIsoToUnix:
    def test_z_and_offset_forms_agree(self):
        expected = _unix(2026, 10, 3, 15, 28, 7)
        assert tb.iso_to_unix("2026-10-03T15:28:07Z") == expected
        assert tb.iso_to_unix("2026-10-03T15:28:07+00:00") == expected
        assert tb.iso_to_unix("2026-10-03T17:28:07+02:00") == expected
        assert tb.iso_to_unix(" 2026-10-03T15:28:07.900Z ") == expected

    def test_no_offset_means_utc(self):
        assert tb.iso_to_unix("2026-10-03T15:28:07") == _unix(2026, 10, 3, 15, 28, 7)

    @pytest.mark.parametrize("value", [None, "", "   ", "yesterday", "2026-13-40T00:00:00Z",
                                       1790000000, 1790000000.5, True, [], {}])
    def test_junk(self, value):
        assert tb.iso_to_unix(value) is None


class TestPayloads:
    def test_change_shape(self):
        assert tb.change_payload(_change()) == {
            "commit": COMMIT, "short": "2cd73be", "version": "6.0.19",
            "title": "Clan chat sync works without the API",
            "points": ["Syncs over the webhook", "No API key needed"],
            "date": _unix(2026, 10, 3, 15, 14, 46), "pr": 59,
        }

    def test_change_is_coerced(self):
        change = tb.change_payload({
            "commit": "not a commit; rm -rf", "short": None, "version": 6.0,
            "title": "  ", "subject": "v6.0.19: the subject",
            "points": ["ok", 7, None, "", {"a": 1}, "  padded  "] + ["p"] * 40,
            "date": "soon", "pr": "59",
        })
        assert change["commit"] == "" and change["short"] == ""
        assert change["version"] == "6.0"
        assert change["title"] == "v6.0.19: the subject"   # falls back to the subject
        assert change["points"][:3] == ["ok", "7", "padded"]
        assert len(change["points"]) == tb.MAX_POINTS
        assert change["date"] is None and change["pr"] == 59

    @pytest.mark.parametrize("pr", [None, True, 0, -3, "abc", 1.5e400, [59]])
    def test_change_pr_must_be_a_positive_number(self, pr):
        assert tb.change_payload(_change(pr=pr))["pr"] is None

    def test_change_short_falls_back_to_the_commit(self):
        assert tb.change_payload(_change(short=None))["short"] == COMMIT[:7]

    def test_change_from_nothing(self):
        assert tb.change_payload(None) == {
            "commit": "", "short": "", "version": None, "title": "",
            "points": [], "date": None, "pr": None,
        }

    def test_build_shape(self):
        build = tb.build_payload(_manifest())
        assert build == {
            "build_id": "6.0.19-2cd73be-rl1.13.1",
            "version": "6.0.19",
            "commit": COMMIT,
            "short": "2cd73be",
            "ref": "master",
            "runelite_version": "1.13.1",
            "built_at": _unix(2026, 10, 3, 15, 28, 7),
            "size_bytes": 28563998,
            "sha256": SHA256,
            "reason": "plugin",
            "repo_url": "https://github.com/joelhalen/droptracker-plugin",
            "release": {"commit": RELEASE_COMMIT, "short": "577fa89",
                        "version": "6.0.12", "is_ancestor": True},
            "changes": [tb.change_payload(_change())],
            "since_release": build["since_release"],
        }
        assert [c["title"] for c in build["since_release"]] == [
            "Clan chat sync works without the API", "Older"]
        # Nothing the build job keeps for itself is passed on.
        assert not {"file", "smoke", "previous_build", "schema", "plugin"} & set(build)

    def test_build_without_since_release(self):
        build = tb.build_payload(_manifest(), include_since_release=False)
        assert build["since_release"] == []
        assert len(build["changes"]) == 1

    def test_build_is_coerced(self):
        build = tb.build_payload({
            "build_id": "b1", "plugin": "6.0.19", "runelite_version": 1.13,
            "built_at": 1790000000, "size_bytes": "28563998", "sha256": "abc",
            "reason": None, "repo_url": "javascript:alert(1)", "release": "6.0.12",
            "changes": {"commit": COMMIT}, "since_release": [_change(), "junk", 5, None],
        })
        assert build["version"] == "" and build["commit"] == "" and build["short"] == ""
        assert build["ref"] is None
        assert build["runelite_version"] == "1.13"
        assert build["built_at"] is None
        assert build["size_bytes"] == 28563998
        assert build["sha256"] is None
        assert build["reason"] == ""
        assert build["repo_url"] is None
        assert build["release"] is None
        assert build["changes"] == []
        assert len(build["since_release"]) == 1

    @pytest.mark.parametrize("size", [None, True, -5, "big", float("nan"), float("inf"), [1]])
    def test_build_size_is_never_negative_or_not_a_number(self, size):
        assert tb.build_payload(_manifest(size_bytes=size))["size_bytes"] == 0

    def test_release_flags_must_be_real_booleans(self):
        release = {"commit": RELEASE_COMMIT, "short": "577fa89", "version": None,
                   "is_ancestor": "yes"}
        assert tb.build_payload(_manifest(release=release))["release"] == {
            "commit": RELEASE_COMMIT, "short": "577fa89", "version": None, "is_ancestor": False}
        assert tb.build_payload(_manifest(release={"version": "6.0.12"}))["release"] is None

    def test_lists_are_capped(self):
        many = [_change(title=f"change {n}") for n in range(tb.MAX_CHANGES + 50)]
        build = tb.build_payload(_manifest(changes=many, since_release=many))
        assert len(build["changes"]) == tb.MAX_CHANGES == 200
        assert len(build["since_release"]) == tb.MAX_CHANGES
        assert build["changes"][0]["title"] == "change 0"   # the newest are kept

    def test_build_from_nothing_still_has_every_key(self):
        build = tb.build_payload(None)
        assert build["build_id"] == "" and build["release"] is None
        assert build["changes"] == [] and build["since_release"] == []

    def test_build_summary(self):
        assert tb.build_summary(_manifest()) == {
            "build_id": "6.0.19-2cd73be-rl1.13.1", "version": "6.0.19",
            "runelite_version": "1.13.1", "built_at": _unix(2026, 10, 3, 15, 28, 7),
            "reason": "plugin",
        }
        assert tb.build_summary({"build_id": "b1"}) == {
            "build_id": "b1", "version": None, "runelite_version": None,
            "built_at": None, "reason": None,
        }

    def test_status_shape(self):
        status = {
            "checked_at": "2026-10-03T15:40:00Z", "state": "failed", "ref": "master",
            "head_commit": COMMIT, "runelite_version": "1.13.1",
            "release_commit": RELEASE_COMMIT, "current_build_id": "6.0.18-bbbbbbb-rl1.13.1",
            "failed": {"commit": COMMIT, "runelite_version": "1.13.1", "at": 1790000000.0},
            "error": "gradle exited 1", "log_tail": "a very long log",
        }
        assert tb.status_payload(status) == {
            "state": "failed", "checked_at": _unix(2026, 10, 3, 15, 40, 0), "ref": "master",
            "head_commit": COMMIT, "runelite_version": "1.13.1", "error": "gradle exited 1",
            "current_build_id": "6.0.18-bbbbbbb-rl1.13.1",
        }

    @pytest.mark.parametrize("status", [None, "ok", [], {}, {"state": "exploded"},
                                        {"state": ["ok"]}, {"state": None}])
    def test_status_unknown(self, status):
        assert tb.status_payload(status) == {
            "state": "unknown", "checked_at": None, "ref": None, "head_commit": None,
            "runelite_version": None, "error": None, "current_build_id": None,
        }

    def test_status_error_is_bounded(self):
        assert len(tb.status_payload({"state": "failed", "error": "x" * 50_000})["error"]) == 2000

    def test_release_version(self):
        assert tb.release_version(_manifest()) == "6.0.12"
        assert tb.release_version(_manifest(release=None)) is None
        assert tb.release_version(_manifest(release={"commit": RELEASE_COMMIT,
                                                     "version": None})) is None
        assert tb.release_version(None) is None
        assert tb.release_version({"release": "6.0.12"}) is None
