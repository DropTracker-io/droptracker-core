"""Unit tests for the pure/change-gating parts of utils/github.py — the pieces
that decide WHETHER the GitHub Pages content repo gets a commit. The old
implementation committed every cycle (re-encrypted ciphertext + unconditional
dated-file writes); these tests pin the gating logic that fixed that.

Loaded standalone via importlib (conftest stubs db/utils; github + aiohttp are
real venv packages).
"""
import importlib.util
import json
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


gh = _load("_github_publisher_under_test", "utils", "github.py")


class TestGitBlobSha:
    def test_matches_git_hash_object(self):
        # `printf 'hello\n' | git hash-object --stdin`
        assert gh._git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"

    def test_differs_on_content_change(self):
        assert gh._git_blob_sha("1,2,3") != gh._git_blob_sha("1,2,4")


class TestStaleDatedPaths:
    PATHS = [
        "content/core.json",
        "content/news.txt",
        "content/valued_items.txt",
        "content/untradeable_items.txt",
        "content/20260722.json",
        "content/20260722-1.json",
        "content/20260722-k.txt",
        "content/20260701.json",
        "content/20260701-k.txt",
        "content/20250101.json",
    ]

    def test_only_old_dated_files_returned(self):
        stale = gh._stale_dated_paths(self.PATHS, "20260722", keep_days=7)
        assert sorted(stale) == [
            "content/20250101.json",
            "content/20260701-k.txt",
            "content/20260701.json",
        ]

    def test_non_dated_files_never_pruned(self):
        stale = gh._stale_dated_paths(self.PATHS, "20990101", keep_days=1)
        assert "content/core.json" not in stale
        assert "content/news.txt" not in stale
        assert "content/valued_items.txt" not in stale
        assert "content/untradeable_items.txt" not in stale

    def test_boundary_is_exclusive_of_keep_window(self):
        # exactly keep_days old -> kept; one day older -> pruned
        stale = gh._stale_dated_paths(
            ["content/20260715.json", "content/20260714.json"], "20260722", keep_days=7)
        assert stale == ["content/20260714.json"]


class TestWebhookSetChanged:
    def _updater(self):
        return gh.GithubPagesUpdater.__new__(gh.GithubPagesUpdater)

    def _file(self, entries):
        return SimpleNamespace(decoded_content=json.dumps(entries).encode("utf-8"))

    def test_missing_file_is_changed(self):
        assert self._updater()._webhook_set_changed(None, ["x"]) is True

    def test_same_decrypted_set_is_unchanged(self, monkeypatch):
        # Different ciphertexts (cipher-A vs cipher-B) decrypting to the same
        # urls must NOT count as a change — that was the every-cycle-commit bug.
        mapping = {"cipherA1": "url1", "cipherA2": "url2",
                   "cipherB1": "url1", "cipherB2": "url2"}
        monkeypatch.setattr(gh, "decrypt_webhook", mapping.__getitem__)
        changed = self._updater()._webhook_set_changed(
            self._file(["cipherA1", "cipherA2"]), ["cipherB2", "cipherB1"])
        assert changed is False

    def test_different_url_set_is_changed(self, monkeypatch):
        mapping = {"a": "url1", "b": "url2", "c": "url3"}
        monkeypatch.setattr(gh, "decrypt_webhook", mapping.__getitem__)
        assert self._updater()._webhook_set_changed(self._file(["a", "b"]), ["a", "c"]) is True

    def test_length_change_is_changed(self, monkeypatch):
        monkeypatch.setattr(gh, "decrypt_webhook", lambda entry: entry)
        assert self._updater()._webhook_set_changed(self._file(["a"]), ["a", "b"]) is True

    def test_undecryptable_existing_content_is_changed(self, monkeypatch):
        def _boom(entry):
            raise ValueError("bad token")
        monkeypatch.setattr(gh, "decrypt_webhook", _boom)
        assert self._updater()._webhook_set_changed(self._file(["a"]), ["a"]) is True

    def test_non_json_existing_content_is_changed(self):
        bad = SimpleNamespace(decoded_content=b"<html>error</html>")
        assert self._updater()._webhook_set_changed(bad, ["a"]) is True
