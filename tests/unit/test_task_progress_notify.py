"""Unit tests for the per-task progress-notification override
(``EventTask.config.progress_notify`` — 'off' | 'milestones' | 'all').

The override replaces the event/team ``task_progress`` verbosity for one task,
on BOTH the plugin inbox fan-out and the Discord enqueue, so a bulk-quantity
tile ("collect 15k granite dust") can announce at 25/50/75% while a set tile
("full inquisitor") announces every piece.

Loaded straight from the file (test_event_engine_scoring pattern) so the
conftest sys.modules stubs never interfere; the collaborators
``_maybe_enqueue_progress`` imports lazily are monkeypatched per-test.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_spec = importlib.util.spec_from_file_location(
    "_event_engine_progress_ut", os.path.join(_ROOT, "services", "event_engine.py"))
engine = importlib.util.module_from_spec(_spec)
sys.modules["_event_engine_progress_ut"] = engine
_spec.loader.exec_module(engine)

_notif_spec = importlib.util.spec_from_file_location(
    "_event_notifications_progress_ut",
    os.path.join(_ROOT, "services", "event_notifications.py"))
event_notifications = importlib.util.module_from_spec(_notif_spec)
sys.modules["_event_notifications_progress_ut"] = event_notifications
_notif_spec.loader.exec_module(event_notifications)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture()
def gates(monkeypatch):
    """Wire _maybe_enqueue_progress's lazy imports to recorders; returns
    (plugin_recorder, discord_recorder, set_team_mode)."""
    plugin = Recorder()
    discord = Recorder()
    team = {"mode": "off"}
    monkeypatch.setitem(
        sys.modules, "services.event_notifications", event_notifications)
    monkeypatch.setitem(
        sys.modules, "services.plugin_notifications",
        SimpleNamespace(fan_out_event_notification=plugin,
                        resolve_item_icon_id=lambda *a, **k: None))
    monkeypatch.setitem(
        sys.modules, "services.event_team_discord",
        SimpleNamespace(team_progress_interest=lambda *a, **k: team["mode"]))
    monkeypatch.setattr(engine, "_enqueue_notification", discord)
    return plugin, discord, (lambda mode: team.update(mode=mode))


def _event(mode="off", toggle=True):
    return {"id": 1, "name": "Test event", "group_id": 2,
            "message_config": {"task_progress": mode,
                              "toggles": {"event_task_progress": toggle}}}


def _task(progress_notify=None, ttype="item_collection"):
    config = {"kind": "any_of", "items": [{"item_name": "Granite dust"}]}
    if progress_notify is not None:
        config["progress_notify"] = progress_notify
    return {"id": 7, "label": "Collect granite dust", "type": ttype,
            "config": config, "target_value": 15000}


def _run(gates_tuple, task, event, previous, current, threshold=15000):
    engine._maybe_enqueue_progress(
        object(), event, task, team_id=3, player_id=4, player_name="Alice",
        previous=previous, current=current, threshold=threshold)


# ── task_progress_notify_mode ────────────────────────────────────────────────

def test_mode_reads_dict_and_json_configs():
    assert engine.task_progress_notify_mode(
        {"config": {"progress_notify": "milestones"}}) == "milestones"
    assert engine.task_progress_notify_mode(
        {"config": '{"progress_notify": "all"}'}) == "all"
    assert engine.task_progress_notify_mode({"config": None}) is None
    assert engine.task_progress_notify_mode({"config": {"kind": "any_of"}}) is None
    assert engine.task_progress_notify_mode(
        {"config": {"progress_notify": "sometimes"}}) is None


# ── inherit (no override) — pre-existing behaviour is unchanged ──────────────

def test_inherit_plugin_every_increment_discord_muted(gates):
    plugin, discord, _ = gates
    _run(gates, _task(), _event(mode="off"), previous=10, current=20)
    assert len(plugin.calls) == 1
    assert not discord.calls


def test_inherit_all_mode_enqueues_discord(gates):
    plugin, discord, _ = gates
    _run(gates, _task(), _event(mode="all"), previous=10, current=20)
    assert len(plugin.calls) == 1
    assert len(discord.calls) == 1


def test_inherit_team_mode_still_maxes(gates):
    plugin, discord, set_team = gates
    set_team("all")
    _run(gates, _task(), _event(mode="off"), previous=10, current=20)
    assert len(discord.calls) == 1  # per-team channel wants every increment


# ── override: off ────────────────────────────────────────────────────────────

def test_off_override_silences_plugin_and_discord(gates):
    plugin, discord, set_team = gates
    set_team("all")
    _run(gates, _task("off"), _event(mode="all"), previous=10, current=20)
    assert not plugin.calls
    assert not discord.calls


# ── override: milestones ─────────────────────────────────────────────────────

def test_milestones_override_drops_non_crossing_increment(gates):
    plugin, discord, _ = gates
    # 10 → 20 of 15000 crosses nothing.
    _run(gates, _task("milestones"), _event(mode="all"), previous=10, current=20)
    assert not plugin.calls
    assert not discord.calls


def test_milestones_override_fires_on_crossing(gates):
    plugin, discord, _ = gates
    # 3700 → 3800 crosses 25% of 15000 (3750).
    _run(gates, _task("milestones"), _event(mode="off"), previous=3700, current=3800)
    assert len(plugin.calls) == 1
    assert len(discord.calls) == 1
    payload = plugin.calls[0][0][3]
    assert payload["milestone_pct"] == 25
    assert payload["progress_notify"] == "milestones"


# ── override: all ────────────────────────────────────────────────────────────

def test_all_override_beats_event_off_mode(gates):
    plugin, discord, _ = gates
    _run(gates, _task("all"), _event(mode="off"), previous=10, current=20)
    assert len(plugin.calls) == 1
    assert len(discord.calls) == 1
    # The override rides in the payload so the sender's per-destination
    # verbosity re-checks honor it too.
    assert discord.calls[0][0][4]["progress_notify"] == "all"


def test_all_override_keeps_meter_task_step(gates):
    plugin, discord, _ = gates
    # xp_target: 100 → 200 of 15000 crosses no 10% step — the plugin fan-out
    # stays stepped even under 'all' (per-XP-drop chat spam), Discord posts.
    _run(gates, _task("all", ttype="xp_target"), _event(mode="off"),
         previous=100, current=200)
    assert not plugin.calls
    assert len(discord.calls) == 1


def test_event_toggle_still_mutes_discord_not_plugin(gates):
    plugin, discord, _ = gates
    _run(gates, _task("all"), _event(mode="all", toggle=False),
         previous=10, current=20)
    assert len(plugin.calls) == 1  # plugin inbox is independent of Discord
    assert not discord.calls


# ── _enqueue_notification override gate ──────────────────────────────────────

class _Nested:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Session:
    def __init__(self):
        self.added = []

    def begin_nested(self):
        return _Nested()

    def add(self, row):
        self.added.append(row)

    def flush(self):
        pass


def _wire_enqueue(monkeypatch, team_interest=False):
    monkeypatch.setitem(
        sys.modules, "services.event_notifications", event_notifications)
    monkeypatch.setitem(
        sys.modules, "services.plugin_notifications",
        SimpleNamespace(fan_out_event_notification=Recorder()))
    monkeypatch.setitem(
        sys.modules, "services.event_team_discord",
        SimpleNamespace(team_channel_interest=lambda *a, **k: team_interest))
    monkeypatch.setitem(
        sys.modules, "db.models.notification_queue",
        SimpleNamespace(NotificationQueue=lambda **k: SimpleNamespace(**k)))


def test_enqueue_gate_lets_override_past_off_mode(monkeypatch):
    _wire_enqueue(monkeypatch)
    session = _Session()
    engine._enqueue_notification(
        session, "event_task_progress", _event(mode="off"), 4,
        {"task_id": 7, "team_id": 3, "progress_notify": "all"})
    assert len(session.added) == 1


def test_enqueue_gate_toggle_off_still_blocks_override(monkeypatch):
    _wire_enqueue(monkeypatch, team_interest=False)
    session = _Session()
    engine._enqueue_notification(
        session, "event_task_progress", _event(mode="off", toggle=False), 4,
        {"task_id": 7, "team_id": 3, "progress_notify": "all"})
    assert not session.added
