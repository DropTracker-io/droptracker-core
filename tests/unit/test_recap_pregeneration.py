"""Clan cards are generated at local midnight, not at the post hour.

Group cards are the half of the recap feature that is NOT built on first view —
`web_api/routes/recaps.py` generates only SCOPE_PLAYER and answers 404 for a
missing group card. So until the delivery run reached a clan, its public
`/groups/{id}/recap/{period}` URL was dead: for an America/Chicago clan that
meant 17:00 UTC on the 1st, twelve hours after the month closed.

`generate_group_cards` closes that window. What matters here is that it is
generation and NOT delivery — it must never send, never consult the delivery
ledger, and never move with `recap_post_hour`.

Loaded from the file path so the conftest `services` stub doesn't shadow it.
"""

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timezone

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "recap_delivery.py",
)
_spec = importlib.util.spec_from_file_location("_recap_pregen_under_test", _MODULE_PATH)
delivery = importlib.util.module_from_spec(_spec)
sys.modules["_recap_pregen_under_test"] = delivery
_spec.loader.exec_module(delivery)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class _FakeSession:
    """Just enough session for the selection query."""

    def __init__(self, group_ids):
        self._group_ids = list(group_ids)
        self.rolled_back = 0

    def execute(self, *_a, **_k):
        return _FakeResult([(g,) for g in self._group_ids])

    def rollback(self):
        self.rolled_back += 1


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def _wire(monkeypatch, *, cfg, group_ids, existing=()):
    """Point the module at a fake group_config and snapshot store."""
    import types

    fake_gc = types.SimpleNamespace(
        get_bulk=lambda session, ids, keys: cfg,
        is_truthy=lambda v: str(v).strip().lower() in ("1", "true", "yes", "on"),
    )
    # `from utils import group_config` reads the attribute off the package, so
    # patching sys.modules alone would leave the real module bound.
    import utils
    monkeypatch.setattr(utils, "group_config", fake_gc)
    have = set(existing)
    monkeypatch.setattr(
        delivery, "snapshot_exists",
        lambda session, scope, subject_id, period: subject_id in have,
    )
    return _FakeSession(group_ids)


class TestGenerationWindow:
    """Local midnight on the 1st — the day's own boundary."""

    def test_utc_clan_is_eligible_the_moment_the_month_closes(self, monkeypatch):
        cfg = {(14, delivery.CFG_ENABLED): "1", (14, delivery.CFG_TIMEZONE): "UTC"}
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 1, 0, 1)
        ) == [14]

    def test_not_eligible_before_its_month_has_closed(self, monkeypatch):
        # The one mistake a recap cannot walk back: a card for a running month.
        cfg = {(14, delivery.CFG_ENABLED): "1", (14, delivery.CFG_TIMEZONE): "UTC"}
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 8, 31, 23, 59)
        ) == []

    def test_western_clan_waits_for_its_own_midnight(self, monkeypatch):
        # Chicago is UTC-5 in September: local midnight is 05:00 UTC.
        cfg = {(14, delivery.CFG_ENABLED): "1",
               (14, delivery.CFG_TIMEZONE): "America/Chicago"}
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 1, 4, 59)
        ) == []
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 1, 5, 1)
        ) == [14]

    def test_eastern_clan_is_clamped_to_the_month_close(self, monkeypatch):
        # Sydney's local midnight on the 1st is 14:00 UTC on Aug 31 — before the
        # month it summarises has ended, so it clamps forward rather than back.
        cfg = {(14, delivery.CFG_ENABLED): "1",
               (14, delivery.CFG_TIMEZONE): "Australia/Sydney"}
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 8, 31, 23, 0)
        ) == []
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 1, 0, 1)
        ) == [14]

    def test_post_hour_does_not_move_generation(self, monkeypatch):
        # A clan posting at 20:00 must not have its archive hidden until 20:00.
        cfg = {
            (14, delivery.CFG_ENABLED): "1",
            (14, delivery.CFG_TIMEZONE): "UTC",
            (14, delivery.CFG_HOUR): "20",
        }
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 1, 0, 5)
        ) == [14]

    def test_stays_eligible_well_past_the_delivery_grace_window(self, monkeypatch):
        # Delivery is bounded by grace_days so a late run cannot surprise a clan
        # with a post. Nothing is surprised by a card that merely exists.
        cfg = {(14, delivery.CFG_ENABLED): "1", (14, delivery.CFG_TIMEZONE): "UTC"}
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 20, 12)
        ) == [14]

    def test_disabled_clan_is_never_generated(self, monkeypatch):
        cfg = {(14, delivery.CFG_ENABLED): "0", (14, delivery.CFG_TIMEZONE): "UTC"}
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 1, 12)
        ) == []

    def test_a_clan_with_nowhere_to_post_still_gets_its_url(self, monkeypatch):
        # No channel configured at all: delivery skips it, generation must not.
        cfg = {(14, delivery.CFG_ENABLED): "1", (14, delivery.CFG_TIMEZONE): "UTC"}
        s = _wire(monkeypatch, cfg=cfg, group_ids=[14])
        assert delivery.collect_group_generation_ids(
            s, "2026-08", _utc(2026, 9, 1, 12)
        ) == [14]


class TestGenerationPass:
    def _wire_pass(self, monkeypatch, *, existing=(), fails=(), apply=True):
        cfg = {}
        ids = [14, 15, 16]
        for g in ids:
            cfg[(g, delivery.CFG_ENABLED)] = "1"
            cfg[(g, delivery.CFG_TIMEZONE)] = "UTC"
        s = _wire(monkeypatch, cfg=cfg, group_ids=ids, existing=existing)

        events = []

        async def fake_harvest(session, period, *, group_ids, player_ids, log=None):
            events.append(("harvest", list(group_ids), list(player_ids)))
            return {}

        def fake_ensure(session, scope, subject_id, period):
            events.append(("card", scope, subject_id))
            if subject_id in fails:
                raise RuntimeError("roster blew up")
            return "2026-09-01T00:00:00"

        import types
        monkeypatch.setitem(
            sys.modules, "services.recap_ehb",
            types.SimpleNamespace(harvest_month_ehb=fake_harvest),
        )
        monkeypatch.setattr(delivery, "ensure_snapshot", fake_ensure)
        built, eligible = asyncio.run(delivery.generate_group_cards(
            s, period="2026-08", now=_utc(2026, 9, 1, 0, 5),
            apply=apply, log=lambda m: None,
        ))
        return events, built, eligible, s

    def test_builds_every_missing_card(self, monkeypatch):
        events, built, eligible, _ = self._wire_pass(monkeypatch)
        assert built == 3 and eligible == 3
        # SCOPE_GROUP comes from the conftest-stubbed `db`, so compare against
        # the module's own constant rather than the literal string.
        assert [e[1:] for e in events if e[0] == "card"] == [
            (delivery.SCOPE_GROUP, 14),
            (delivery.SCOPE_GROUP, 15),
            (delivery.SCOPE_GROUP, 16),
        ]

    def test_an_existing_card_is_never_recomputed(self, monkeypatch):
        events, built, _, _ = self._wire_pass(monkeypatch, existing=(15,))
        assert built == 2
        assert [e[2] for e in events if e[0] == "card"] == [14, 16]

    def test_harvest_runs_before_any_card_is_built(self, monkeypatch):
        # EHB is fetched from outside and ensure_snapshot returns a stored row
        # untouched, so harvesting afterwards would freeze an EHB-less card into
        # both the archive and the noon post.
        events, _, _, _ = self._wire_pass(monkeypatch)
        assert events[0][0] == "harvest"
        assert events[0][1] == [14, 15, 16]

    def test_one_bad_clan_does_not_cost_the_others(self, monkeypatch):
        events, built, _, s = self._wire_pass(monkeypatch, fails=(15,))
        assert built == 2
        assert [e[2] for e in events if e[0] == "card"] == [14, 15, 16]
        assert s.rolled_back == 1

    def test_a_dry_run_writes_nothing(self, monkeypatch):
        events, built, eligible, _ = self._wire_pass(monkeypatch, apply=False)
        assert built == 0 and eligible == 3
        assert events == []

    def test_nothing_to_do_skips_the_harvest_entirely(self, monkeypatch):
        events, built, _, _ = self._wire_pass(monkeypatch, existing=(14, 15, 16))
        assert built == 0
        assert events == []
