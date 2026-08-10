"""sites-v1: SITES_BETA_GROUP_IDS parsing — the staged-rollout allowlist that
grants custom_site to specific groups while every tier keeps it off."""
import importlib.util
import os


def _load_parser():
    # Load db/entitlements.py directly: conftest stubs the db package, and the
    # parser under test needs no package machinery.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "db", "entitlements.py")
    spec = importlib.util.spec_from_file_location("_entitlements_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ent = _load_parser()


def test_empty_unset(monkeypatch):
    monkeypatch.delenv("SITES_BETA_GROUP_IDS", raising=False)
    assert ent._sites_beta_group_ids() == set()


def test_single_id(monkeypatch):
    monkeypatch.setenv("SITES_BETA_GROUP_IDS", "14")
    assert ent._sites_beta_group_ids() == {14}


def test_commas_spaces_and_junk(monkeypatch):
    monkeypatch.setenv("SITES_BETA_GROUP_IDS", "14, 27 abc ,3,")
    assert ent._sites_beta_group_ids() == {14, 27, 3}
