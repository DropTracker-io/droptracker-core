"""Pure-logic tests for the Activity launch handler. The HTTP/gateway paths
need a live interaction, so only the pure predicate + message builder are
covered here. Loads the module by file path with the REAL interactions package
(conftest stubs `interactions`, but the module subclasses its Extension, so we
un-stub it just for this import)."""
import importlib.util
import os
import sys


def _load_activity_launch():
    # Drop the conftest stub so the real (installed) interactions package loads;
    # the module subclasses interactions.Extension at import time.
    for name in [m for m in sys.modules if m == "interactions" or m.startswith("interactions.")]:
        del sys.modules[name]
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "services", "activity_launch.py")
    spec = importlib.util.spec_from_file_location("_activity_launch_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


al = _load_activity_launch()


def test_only_entry_point_command_matches():
    assert al.is_entry_point_interaction({"type": 2, "data": {"type": 4, "name": "launch"}})
    # A normal slash command (interaction type 2, command type 1) is ignored.
    assert not al.is_entry_point_interaction({"type": 2, "data": {"type": 1, "name": "help"}})
    # A component interaction (type 3) is ignored.
    assert not al.is_entry_point_interaction({"type": 3, "data": {"custom_id": "x"}})
    # Malformed payloads don't raise.
    assert not al.is_entry_point_interaction({})
    assert not al.is_entry_point_interaction({"type": 2})
    assert not al.is_entry_point_interaction(None)


def test_launch_message_ephemeral_by_default():
    payload = al.build_launch_message({})
    assert payload is not None
    assert payload["embeds"][0]["title"]
    # 1<<6 = ephemeral flag, present because LAUNCH_MESSAGE_EPHEMERAL defaults True.
    assert payload.get("flags") == al._MSG_FLAG_EPHEMERAL == 64


def test_public_message_has_no_flags(monkeypatch):
    monkeypatch.setattr(al, "LAUNCH_MESSAGE_EPHEMERAL", False)
    payload = al.build_launch_message({})
    assert "flags" not in payload


def test_callback_constant_is_launch_activity():
    assert al._CALLBACK_LAUNCH_ACTIVITY == 12
