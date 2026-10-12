"""Phase 2 of suggestion #198: the Submit button and Discord review buttons.

Pure modules only (services/submit_panel_core.py, services/review_buttons_core.py,
the launcher reconciler's variant handling, the outbox's action buttons, the
review ping and the ``role`` config type); the Discord shells are thin and are
exercised by real clicks.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, _ROOT.joinpath(*parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sp = _load("_sp_core_under_test", "services", "submit_panel_core.py")
rb = _load("_rb_core_under_test", "services", "review_buttons_core.py")
launch = _load("_launch_core_for_variants", "services", "activity_launch_core.py")


# ── Submit modal ─────────────────────────────────────────────────────────────

class TestBuildModal:
    @pytest.mark.parametrize("sub_type", ["drop", "clog", "pb", "ca", "pet"])
    def test_every_type_fits_discords_five_component_cap(self, sub_type):
        modal = sp.build_modal(sub_type, ["Alt one", "Main"], with_upload=True)
        assert len(modal["components"]) <= 5
        assert modal["custom_id"] == f"dtsubmit:modal:{sub_type}"
        assert len(modal["title"]) <= 45
        kinds = [c["component"]["type"] for c in modal["components"]]
        assert kinds[0] == 3            # account picker first (two accounts)
        assert kinds[-1] == 19          # screenshot upload last

    def test_one_account_has_no_picker(self):
        modal = sp.build_modal("drop", ["Main"])
        ids = [c["component"]["custom_id"] for c in modal["components"]]
        assert "account" not in ids and ids[:3] == ["item", "npc", "quantity"]

    def test_plain_modal_has_no_upload(self):
        modal = sp.build_modal("pb", ["Main"], with_upload=False)
        assert all(c["component"]["type"] != 19 for c in modal["components"])

    def test_every_component_is_label_wrapped(self):
        for c in sp.build_modal("ca", ["a", "b"])["components"]:
            assert c["type"] == 18 and c["label"]

    def test_modal_type_round_trip(self):
        assert sp.modal_type(sp.modal_custom_id("pet")) == "pet"
        assert sp.modal_type("dtsubmit:modal:quest") is None
        assert sp.modal_type("evtsignup:1") is None


class TestParseModalSubmit:
    def test_label_shape_with_select_and_upload(self):
        data = {
            "custom_id": "dtsubmit:modal:drop",
            "components": [
                {"type": 18, "id": 1, "component": {"type": 3, "custom_id": "account", "values": ["Main"]}},
                {"type": 18, "id": 2, "component": {"type": 4, "custom_id": "item", "value": "Abyssal whip"}},
                {"type": 18, "id": 3, "component": {"type": 4, "custom_id": "npc", "value": "Abyssal demon"}},
                {"type": 18, "id": 4, "component": {"type": 4, "custom_id": "quantity", "value": ""}},
                {"type": 18, "id": 5, "component": {"type": 19, "custom_id": "proof", "values": ["999"]}},
            ],
            "resolved": {"attachments": {"999": {"url": "https://cdn.discordapp.com/x.png",
                                                 "content_type": "image/png", "size": 10}}},
        }
        values, attachments = sp.parse_modal_submit(data)
        assert values == {"account": "Main", "item": "Abyssal whip",
                          "npc": "Abyssal demon", "quantity": ""}
        assert attachments[0]["url"].endswith("x.png")

    def test_classic_action_row_shape(self):
        data = {"components": [{"type": 1, "components": [
            {"type": 4, "custom_id": "item", "value": "Dragon pickaxe"}]}]}
        assert sp.parse_modal_submit(data) == ({"item": "Dragon pickaxe"}, [])

    def test_upload_without_resolved_attachment_is_ignored(self):
        data = {"components": [{"type": 18, "component": {"type": 19, "custom_id": "proof",
                                                          "values": ["1"]}}]}
        assert sp.parse_modal_submit(data) == ({}, [])

    def test_flatten_for_the_library_parser(self):
        flat = sp.flatten_modal_components([
            {"type": 18, "component": {"type": 4, "custom_id": "item", "value": "Whip"}},
            {"type": 18, "component": {"type": 19, "custom_id": "proof", "values": ["1", "2"]}},
            {"type": 1, "components": [{"type": 4, "custom_id": "npc", "value": "Demon"}]},
        ])
        assert [(r["components"][0]["custom_id"], r["components"][0]["value"]) for r in flat] == [
            ("item", "Whip"), ("proof", "1,2"), ("npc", "Demon")]


def _parse_ms(raw):
    from data.submissions.manual_discord import parse_kill_time_ms

    return parse_kill_time_ms(raw)


class TestPayloadFields:
    def test_drop(self):
        assert sp.payload_fields("drop", {"item": " Whip ", "npc": "Demon", "quantity": "2"},
                                 _parse_ms) == {"item_name": "Whip", "npc_name": "Demon", "quantity": 2}

    def test_drop_bad_quantity(self):
        with pytest.raises(ValueError):
            sp.payload_fields("drop", {"item": "Whip", "npc": "Demon", "quantity": "two"}, _parse_ms)
        with pytest.raises(ValueError):
            sp.payload_fields("drop", {"item": "Whip", "npc": "Demon", "quantity": "0"}, _parse_ms)

    def test_pb_needs_a_real_time(self):
        assert sp.payload_fields("pb", {"npc": "Vorkath", "time": "1:02.4"}, _parse_ms)["time_ms"] == 62400
        with pytest.raises(ValueError):
            sp.payload_fields("pb", {"npc": "Vorkath", "time": "soon"}, _parse_ms)

    def test_ca_tier(self):
        out = sp.payload_fields("ca", {"task": "Faithless Crypt Run", "tier": "Elite"}, _parse_ms)
        assert out == {"task": "Faithless Crypt Run", "tier": "elite"}
        with pytest.raises(ValueError):
            sp.payload_fields("ca", {"task": "x", "tier": "legendary"}, _parse_ms)

    def test_pet_kc(self):
        assert sp.payload_fields("pet", {"item": "Pet snakeling", "kc": "1,203"},
                                 _parse_ms) == {"item_name": "Pet snakeling", "npc_name": None, "kc": 1203}


def test_panel_variant_tracks_the_text():
    assert sp.panel_variant(None) == sp.panel_variant("") == sp.panel_variant("  ")
    assert sp.panel_variant("Send loot!") != sp.panel_variant(None)
    assert sp.panel_text(" ") == sp.DEFAULT_PANEL_MESSAGE


# ── review buttons ───────────────────────────────────────────────────────────

class TestParseCustomId:
    def test_drop_and_event(self):
        assert rb.parse_custom_id("dtrv:m:a:7:123") == {
            "kind": "drop", "action": "approve", "group_id": 7, "drop_id": 123}
        assert rb.parse_custom_id("dtrv:m:u:7:123")["action"] == "undo"
        assert rb.parse_custom_id("dtrv:e:r:55") == {
            "kind": "event", "action": "reject", "completion_id": 55}

    @pytest.mark.parametrize("bad", ["dtrv:e:u:5", "dtrv:m:a:x:1", "dtrv:m:a:7", "evtsignup:1",
                                     None, "dtrv:z:a:1"])
    def test_garbage(self, bad):
        assert rb.parse_custom_id(bad) is None

    def test_builder_round_trip(self):
        assert rb.parse_custom_id(rb.drop_custom_id("reject", 7, 9))["action"] == "reject"


class TestMayReview:
    BASE = dict(interaction_guild_id="100", group_guild_id="100", is_group_admin=False)

    def test_group_admin_always(self):
        assert rb.may_review(**{**self.BASE, "interaction_guild_id": "999", "is_group_admin": True})

    def test_event_manager(self):
        assert rb.may_review(**self.BASE, is_event_manager=True)

    def test_manage_server_in_the_groups_own_server(self):
        assert rb.may_review(**self.BASE, member_permissions=str(1 << 5))
        assert not rb.may_review(**{**self.BASE, "interaction_guild_id": "200"},
                                 member_permissions=str(1 << 5))

    def test_review_role(self):
        assert rb.may_review(**self.BASE, member_role_ids=["5", "77"], review_role_id="77")
        assert not rb.may_review(**self.BASE, member_role_ids=["5"], review_role_id="77")
        assert not rb.may_review(**self.BASE, member_role_ids=["77"], review_role_id=None)

    def test_role_in_another_server_does_not_count(self):
        assert not rb.may_review(**{**self.BASE, "interaction_guild_id": "200"},
                                 member_role_ids=["77"], review_role_id="77")

    def test_ordinary_member(self):
        assert not rb.may_review(**self.BASE, member_permissions="0", member_role_ids=["5"])
        assert not rb.may_review(interaction_guild_id=None, group_guild_id=None,
                                 is_group_admin=False, member_permissions=str(1 << 5))


def _ping(content="🔍 **Manual submission awaiting review** — x"):
    return {
        "flags": 0,
        "content": content,
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Approve", "custom_id": "dtrv:m:a:7:1"},
            {"type": 2, "style": 4, "label": "Reject", "custom_id": "dtrv:m:r:7:1"},
            {"type": 2, "style": 5, "label": "Open the review queue", "url": "https://x"},
        ]}],
    }


class TestDecidedMessage:
    def test_plain_ping_keeps_link_row_without_review_buttons(self):
        # A row holding a review button goes as a whole (link included), and
        # the Undo row takes its place.
        edit = rb.decided_message(_ping(), "✅ Approved by <@1>", "dtrv:m:u:7:1")
        assert edit["content"].endswith("✅ Approved by <@1>")
        ids = [c.get("custom_id") for row in edit["components"] for c in row["components"]]
        assert ids == ["dtrv:m:u:7:1"]

    def test_redecision_replaces_the_status_line(self):
        once = rb.decided_message(_ping(), "✅ Approved by <@1>")
        twice = rb.decided_message({**_ping(), "content": once["content"]}, "❌ Rejected by <@2>")
        assert "Approved" not in twice["content"] and twice["content"].endswith("Rejected by <@2>")

    def test_v2_card_gets_a_text_display_inside_its_container(self):
        card = {"flags": 1 << 15, "components": [{"type": 17, "components": [
            {"type": 10, "content": "### Completion awaiting review"},
            {"type": 1, "components": [{"type": 2, "style": 5, "label": "Open", "url": "https://x"}]},
            {"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Approve", "custom_id": "dtrv:e:a:5"},
                {"type": 2, "style": 4, "label": "Reject", "custom_id": "dtrv:e:r:5"}]},
        ]}]}
        edit = rb.decided_message(card, "✅ Approved by <@1>")
        assert "content" not in edit
        inner = edit["components"][0]["components"]
        assert inner[-1] == {"type": 10, "content": "✅ Approved by <@1>"}
        assert all("dtrv:" not in json.dumps(c) for c in inner)
        assert any(c.get("type") == 1 for c in inner)  # the link row survived

    def test_undo_restores_approve_and_reject(self):
        decided = rb.decided_message(_ping(), "✅ Approved by <@1>", "dtrv:m:u:7:1")
        message = {"flags": 0, "content": decided["content"], "components": decided["components"]}
        edit = rb.undone_message(message, "↩️ <@1> took that back.", 7, 1)
        ids = [c.get("custom_id") for row in edit["components"] for c in row["components"]]
        assert ids == ["dtrv:m:a:7:1", "dtrv:m:r:7:1"]
        assert edit["content"].endswith("took that back.") and "Approved" not in edit["content"]


# ── standing card variants (launcher + Submit card) ──────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestReconcileVariants:
    def _fx(self, edit_ok=True):
        return MagicMock(), AsyncMock(return_value=MagicMock(id=999)), AsyncMock(), \
            AsyncMock(return_value=edit_ok)

    def test_same_variant_is_a_no_op(self):
        s, post, delete, edit = self._fx()
        _run(launch.reconcile_group(s, 5, "42", MagicMock(config_value="42:100"),
                                    post_card=post, delete_message=delete,
                                    variant="s1", variant_row=MagicMock(config_value="s1"),
                                    edit_card=edit))
        post.assert_not_awaited(); edit.assert_not_awaited(); s.commit.assert_not_called()

    def test_changed_variant_edits_in_place(self):
        s, post, delete, edit = self._fx()
        row = MagicMock(config_value="s0")
        _run(launch.reconcile_group(s, 5, "42", MagicMock(config_value="42:100"),
                                    post_card=post, delete_message=delete,
                                    variant="s1", variant_row=row, edit_card=edit))
        edit.assert_awaited_once_with("42", "100", "s1")
        post.assert_not_awaited()
        assert row.config_value == "s1"

    def test_legacy_card_assumed_plain(self):
        s, post, delete, edit = self._fx()
        _run(launch.reconcile_group(s, 5, "42", MagicMock(config_value="42:100"),
                                    post_card=post, delete_message=delete,
                                    variant="s0", variant_row=None, edit_card=edit,
                                    assume_variant="s0"))
        edit.assert_not_awaited(); post.assert_not_awaited()

    def test_failed_edit_posts_a_fresh_card(self):
        s, post, delete, edit = self._fx(edit_ok=False)
        ref = MagicMock(config_value="42:100")
        _run(launch.reconcile_group(s, 5, "42", ref, post_card=post, delete_message=delete,
                                    variant="s1", variant_row=None, edit_card=edit,
                                    assume_variant="s0"))
        post.assert_awaited_once_with("42", "s1")
        delete.assert_not_awaited()
        assert ref.config_value == "42:999"

    def test_new_card_records_its_variant_under_the_given_key(self, monkeypatch):
        import db.models
        monkeypatch.setattr(db.models, "GroupConfiguration",
                            lambda **kw: SimpleNamespace(**kw), raising=False)
        s, post, delete, edit = self._fx()
        _run(launch.reconcile_group(s, 5, "42", None, post_card=post, delete_message=delete,
                                    variant="mabc", variant_row=None, edit_card=edit,
                                    ref_key="submit_panel_message_id",
                                    variant_key="submit_panel_variant"))
        added = [c.args[0] for c in s.add.call_args_list]
        keys = {getattr(a, "config_key", None) for a in added}
        assert {"submit_panel_message_id", "submit_panel_variant"} <= keys

    def test_clearing_forgets_the_variant(self):
        s, post, delete, edit = self._fx()
        ref, var = MagicMock(config_value="42:100"), MagicMock(config_value="s1")
        _run(launch.reconcile_group(s, 5, None, ref, post_card=post, delete_message=delete,
                                    variant="s0", variant_row=var, edit_card=edit))
        delete.assert_awaited_once_with("42", "100")
        s.delete.assert_any_call(ref)
        s.delete.assert_any_call(var)


# ── outbox action buttons + the review ping ──────────────────────────────────

class _Button:
    def __init__(self, style=None, label=None, url=None, custom_id=None):
        self.style, self.label, self.url, self.custom_id = style, label, url, custom_id


class _Row:
    def __init__(self, *buttons):
        self.buttons = buttons


@pytest.fixture
def outbox(monkeypatch):
    fake = MagicMock()
    fake.Button = _Button
    fake.ActionRow = _Row
    fake.ButtonStyle = SimpleNamespace(LINK="link", GREEN="green", RED="red", GREY="grey",
                                       BLURPLE="blurple")
    monkeypatch.setitem(sys.modules, "interactions", fake)
    return _load("_outbox_actions_under_test", "services", "discord_outbox.py")


def test_outbox_builds_only_allowlisted_action_buttons(outbox):
    rows = outbox._build_components(json.dumps([
        {"label": "Approve", "custom_id": "dtrv:m:a:7:1", "style": "success"},
        {"label": "Sneaky", "custom_id": "evtsignup:3"},
        {"label": "Reject", "custom_id": "dtrv:m:r:7:1", "style": "danger"},
        {"label": "Queue", "url": "https://www.droptracker.io/groups/7/submissions"},
    ]))
    got = [(b.label, b.style, b.custom_id) for b in rows[0].buttons]
    assert got == [("Approve", "green", "dtrv:m:a:7:1"), ("Reject", "red", "dtrv:m:r:7:1"),
                   ("Queue", "link", None)]


def test_review_ping_buttons():
    mp = _load("_mp_for_review_buttons", "data", "submissions", "manual_policy.py")
    specs = mp.review_buttons(7, 123)
    assert [s.get("custom_id") for s in specs] == ["dtrv:m:a:7:123", "dtrv:m:r:7:123", None]
    assert specs[-1]["url"].endswith("/groups/7/submissions")
    assert [s.get("url") for s in mp.review_buttons(7, None)] == [
        "https://www.droptracker.io/groups/7/submissions"]


# ── the role config type ─────────────────────────────────────────────────────

class TestRoleConfig:
    def _reg(self):
        import importlib
        return importlib.import_module("web_api.config_registry")

    def test_valid_id_and_mention(self):
        reg = self._reg()
        assert reg.coerce_to_storage("review_role_id", "123456789012345678") == "123456789012345678"
        assert reg.coerce_to_storage("review_role_id", "<@&123456789012345678>") == "123456789012345678"

    def test_clear(self):
        reg = self._reg()
        assert reg.coerce_to_storage("review_role_id", "") == ""
        assert reg.coerce_to_storage("review_role_id", None) == ""

    def test_garbage(self):
        reg = self._reg()
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("review_role_id", "Staff")

    def test_submit_message_is_capped(self):
        reg = self._reg()
        with pytest.raises(reg.ConfigValidationError):
            reg.coerce_to_storage("submit_panel_message", "x" * 251)


# ── services/event_review (shared by the site and the buttons) ───────────────

class _Q:
    def __init__(self, result):
        self.result = result

    def filter(self, *_a, **_k):
        return self

    def with_for_update(self):
        return self

    def first(self):
        return self.result


@pytest.fixture
def review(monkeypatch):
    import db.models
    import services

    calls = []
    comp = SimpleNamespace(id=5, event_id=9, task_id=3, team_id=4, player_id=2,
                           status="pending", quantity=1, source_type="drop",
                           matched_target=None, note=None, acted_by_user_id=None)
    monkeypatch.setattr(db.models, "AuditLog", lambda **kw: ("audit", kw), raising=False)
    engine = SimpleNamespace(
        _task_to_dict=lambda t: {"id": 3},
        apply_completion=lambda s, c: calls.append(("apply", c.status)),
        _publish=lambda *a: None, pending_projection=lambda *a: None,
        publish_event_admin_bump=lambda *a: None)
    receipt = SimpleNamespace(capture=lambda *a, **k: "before",
                              finish=lambda *a, **k: {"delta": 1})
    monkeypatch.setitem(sys.modules, "services.event_engine", engine)
    monkeypatch.setitem(sys.modules, "services.event_credit_receipt", receipt)
    monkeypatch.setattr(services, "event_engine", engine, raising=False)
    monkeypatch.setattr(services, "event_credit_receipt", receipt, raising=False)
    er = _load("_event_review_under_test", "services", "event_review.py")

    added = []
    session = SimpleNamespace(
        query=lambda model: _Q(comp if "Completion" in str(model) else SimpleNamespace(id=3)),
        add=lambda row: added.append(row),
        commit=lambda: calls.append(("commit",)),
    )
    return er, session, comp, calls, added


def test_confirm_applies_audits_and_commits(review):
    er, s, comp, calls, added = review
    ev = SimpleNamespace(id=9, group_id=7, status="active")
    assert er.confirm(s, ev, 5, 42, audit_extra={"via": "discord"}) == {"delta": 1}
    assert comp.status == "confirmed" and comp.acted_by_user_id == 42
    assert calls[:2] == [("apply", "confirmed"), ("commit",)]
    kind, audit = added[0]
    assert audit["action"] == "event.completion.confirm" and audit["actor_user_id"] == 42
    assert json.loads(audit["after"])["via"] == "discord"
    assert json.loads(audit["before"])["status"] == "pending"


def test_confirm_refuses_an_ended_event(review):
    er, s, comp, calls, _ = review
    with pytest.raises(er.ReviewError) as err:
        er.confirm(s, SimpleNamespace(id=9, group_id=7, status="past"), 5, 42)
    assert err.value.status == 409 and comp.status == "pending" and calls == []


def test_confirm_refuses_a_row_already_decided(review):
    er, s, comp, calls, _ = review
    comp.status = "confirmed"
    with pytest.raises(er.ReviewError) as err:
        er.confirm(s, SimpleNamespace(id=9, group_id=7, status="active"), 5, 42)
    assert err.value.status == 409 and calls == []


def test_reject_stores_the_note_without_applying(review):
    er, s, comp, calls, added = review
    er.reject(s, SimpleNamespace(id=9, group_id=7, status="past"), 5, None, note="blurry")
    assert comp.status == "rejected" and comp.note == "blurry"
    assert ("apply", "rejected") not in calls and ("commit",) in calls
    assert added[0][1]["action"] == "event.completion.reject"


def test_audit_snapshot_shape_matches_the_award_routes():
    """event_review.snapshot (confirm/reject) and event_admin._snapshot
    (award/revoke) write one audit shape; this keeps the two copies in step."""
    import importlib

    routes = importlib.import_module("web_api.routes.event_admin")
    er = _load("_event_review_snapshot", "services", "event_review.py")
    comp = SimpleNamespace(status="pending", task_id=3, team_id=4, player_id=2, quantity=2,
                           source_type="drop", matched_target="Whip", note=None,
                           acted_by_user_id=None)
    assert routes._snapshot(comp) == er.snapshot(comp)
