"""scripts/tester_build_report.py: the build job's report to Discord.

The private build job runs this as its last step. Two things matter: what
it says, and that nothing it does can fail the job that called it (it
always exits 0).
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import scripts.tester_build_report as report

BUILD_ID = "6.0.19-2cd73be-rl1.13.1"
COMMIT = "2cd73be4f0a1b2c3d4e5f60718293a4b5c6d7e8f"


def _manifest(titles=("Clan chat sync works without the API",), **over):
    manifest = {
        "schema": 1, "build_id": BUILD_ID, "file": f"droptracker-dev-{BUILD_ID}.zip",
        "reason": "plugin", "plugin": {"version": "6.0.19", "commit": COMMIT},
        "changes": [{"commit": COMMIT, "title": title, "points": []} for title in titles],
    }
    manifest.update(over)
    return manifest


@pytest.fixture()
def reports(monkeypatch):
    """Stands in for services.automation_updates; collects what was reported."""
    seen = []

    def report_run_sync(job, *, ok, changes=None, error=None):
        seen.append({"job": job, "ok": ok, "changes": changes, "error": error})

    monkeypatch.setitem(sys.modules, "services.automation_updates",
                        SimpleNamespace(report_run_sync=report_run_sync))
    # Never the box's real .env.
    monkeypatch.setattr(report, "_load_env", lambda: None)
    return seen


@pytest.fixture()
def publish_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTER_BUILDS_DIR", str(tmp_path))
    return tmp_path


def _status(directory, **status):
    path = directory / "status.json"
    path.write_text(json.dumps(status))
    return str(path)


class TestPublished:
    def test_names_the_build_then_what_changed(self, reports, publish_dir):
        (publish_dir / "current.json").write_text(json.dumps(
            _manifest(titles=("First change", "Second change"))))
        path = _status(publish_dir, state="ok", current_build_id=BUILD_ID)
        assert report.main(["published", path]) == 0
        assert reports == [{
            "job": "tester_build", "ok": True, "error": None,
            "changes": [f"Published {BUILD_ID} (plugin)", "First change", "Second change"],
        }]

    def test_at_most_eight_titles(self, reports, publish_dir):
        (publish_dir / "current.json").write_text(json.dumps(
            _manifest(titles=[f"Change {n}" for n in range(12)])))
        report.main(["published", _status(publish_dir, state="ok")])
        changes = reports[0]["changes"]
        assert len(changes) == 9
        assert changes[1:] == [f"Change {n}" for n in range(8)]

    def test_long_and_empty_titles(self, reports, publish_dir):
        (publish_dir / "current.json").write_text(json.dumps(
            _manifest(titles=("", "x" * 400, "Fine"))))
        report.main(["published", _status(publish_dir, state="ok")])
        _head, long_title, fine = reports[0]["changes"]
        assert len(long_title) == 150 and long_title.endswith("…")
        assert fine == "Fine"

    def test_a_build_with_no_changes_is_still_announced(self, reports, publish_dir):
        (publish_dir / "current.json").write_text(json.dumps(
            _manifest(titles=(), reason="runelite")))
        report.main(["published", _status(publish_dir, state="ok")])
        assert reports[0]["changes"] == [f"Published {BUILD_ID} (runelite)"]

    def test_reads_the_manifest_beside_the_status_file(self, reports, publish_dir, tmp_path_factory):
        # The job's directory is not the one this repo is configured with.
        elsewhere = tmp_path_factory.mktemp("job")
        (elsewhere / "current.json").write_text(json.dumps(_manifest(titles=("From the job",))))
        (publish_dir / "current.json").write_text(json.dumps(
            _manifest(titles=("Stale",), build_id="6.0.1-0000000-rl1")))
        report.main(["published", _status(elsewhere, state="ok")])
        assert reports[0]["changes"] == [f"Published {BUILD_ID} (plugin)", "From the job"]

    def test_falls_back_to_the_configured_directory(self, reports, publish_dir, tmp_path_factory):
        elsewhere = tmp_path_factory.mktemp("job")
        (publish_dir / "current.json").write_text(json.dumps(_manifest()))
        report.main(["published", _status(elsewhere, state="ok")])
        assert reports[0]["changes"][0] == f"Published {BUILD_ID} (plugin)"

    def test_no_manifest_anywhere(self, reports, publish_dir):
        report.main(["published", _status(publish_dir, state="ok", current_build_id=BUILD_ID)])
        report.main(["published", _status(publish_dir, state="ok")])
        assert [r["changes"] for r in reports] == [
            [f"Published {BUILD_ID}"], ["Published a new build"]]
        assert all(r["ok"] for r in reports)


class TestFailed:
    def test_reports_the_error_from_the_status_file(self, reports, publish_dir):
        path = _status(publish_dir, state="failed", error="gradle exited 1\nsecond line",
                       log_tail="a lot of log")
        assert report.main(["failed", path]) == 0
        assert reports == [{"job": "tester_build", "ok": False, "changes": [],
                            "error": "gradle exited 1\nsecond line"}]

    def test_error_is_cut_to_300_characters(self, reports, publish_dir):
        report.main(["failed", _status(publish_dir, state="failed", error="e" * 5000)])
        assert reports[0]["error"] == "e" * 300

    @pytest.mark.parametrize("status", [{"state": "failed"}, {"state": "failed", "error": None},
                                        {"state": "failed", "error": ["not", "text"]}])
    def test_no_recorded_error(self, reports, publish_dir, status):
        report.main(["failed", _status(publish_dir, **status)])
        assert reports[0]["ok"] is False
        assert reports[0]["error"] == "The build failed. No error was recorded."

    def test_unreadable_status_file_still_reports_the_failure(self, reports, publish_dir):
        (publish_dir / "status.json").write_text("{half written")
        report.main(["failed", str(publish_dir / "status.json")])
        report.main(["failed", str(publish_dir / "missing.json")])
        assert [r["ok"] for r in reports] == [False, False]


class TestNeverFailsTheBuildJob:
    @pytest.mark.parametrize("argv", [[], ["published"], ["shipped", "status.json"],
                                      ["failed", "a", "b"]])
    def test_bad_arguments_report_nothing_and_exit_zero(self, reports, argv, capsys):
        assert report.main(argv) == 0
        assert reports == []
        assert "usage:" in capsys.readouterr().out

    def test_reporter_blowing_up_exits_zero(self, reports, publish_dir, monkeypatch, capsys):
        def _boom(job, **kwargs):
            raise RuntimeError("discord is down")

        monkeypatch.setitem(sys.modules, "services.automation_updates",
                            SimpleNamespace(report_run_sync=_boom))
        assert report.main(["failed", _status(publish_dir, state="failed", error="x")]) == 0
        assert "not reported" in capsys.readouterr().out

    def test_reporter_not_importable_exits_zero(self, publish_dir, monkeypatch, capsys):
        # A broken checkout or venv: exactly when the job still needs its exit code.
        monkeypatch.setattr(report, "_load_env", lambda: None)
        monkeypatch.setitem(sys.modules, "services.automation_updates", None)
        assert report.main(["published", _status(publish_dir, state="ok")]) == 0
        assert "not reported" in capsys.readouterr().out

    def test_loading_the_module_does_not_read_the_env_file(self):
        source = open(report.__file__, encoding="utf-8").read()
        module_level = [line for line in source.splitlines()
                        if line.startswith(("load_dotenv", "from dotenv", "import dotenv"))]
        assert module_level == []
