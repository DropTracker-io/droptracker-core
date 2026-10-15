"""A new eternal glory collection log slot is also recorded as a drop.

The amulet of eternal glory comes from charging glories at the Fountain of
Rune and never fires a loot event, so the clog is the only submission we get.
data/submissions/clog_drop_mirror.py turns that new slot into a drop.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import data.submissions.clog_drop_mirror as mirror
from tests.unit.test_clog_multi_slot import _run_processor

ETERNAL_GLORY = 19707

CLOG = {
    "type": "collection_log",
    "player_name": "Some Player",
    "acc_hash": "-123",
    "auth_key": "k",
    "item": "Amulet of eternal glory",
    "item_id": ETERNAL_GLORY,
    "guid": "clog-guid-1",
    "source": "unknown",
    "kc": 0,
    "p_v": "6.1.7",
    "attachment_url": "https://cdn.example/clog.png",
    "attachment_type": "image/png",
}


def test_builds_drop_from_new_slot():
    drop = mirror.build_mirror_drop(CLOG, ETERNAL_GLORY, "Amulet of eternal glory",
                                    "https://img/clog.png")
    assert drop["type"] == "drop"
    assert drop["source"] == "Fountain of Rune"
    assert drop["item_id"] == ETERNAL_GLORY
    assert drop["item_name"] == "Amulet of eternal glory"
    assert drop["quantity"] == 1
    # Server prices it: a 0 client value makes get_true_item_value use GE.
    assert drop["value"] == 0
    # Identity and auth carry over from the clog.
    assert (drop["player_name"], drop["acc_hash"], drop["auth_key"], drop["p_v"]) == (
        "Some Player", "-123", "k", "6.1.7")
    # The clog's stored screenshot is reused, never downloaded again.
    assert drop["downloaded"] is True and drop["image_url"] == "https://img/clog.png"
    assert "attachment_url" not in drop and "kc" not in drop and "item" not in drop


def test_drop_guid_is_stable_and_distinct_from_the_clog():
    a = mirror.build_mirror_drop(CLOG, ETERNAL_GLORY, "Amulet of eternal glory", "")
    b = mirror.build_mirror_drop(dict(CLOG), ETERNAL_GLORY, "Amulet of eternal glory", "")
    assert a["guid"] == b["guid"] != CLOG["guid"]
    assert "downloaded" not in a  # no stored screenshot -> nothing to reuse


def test_other_items_and_guidless_clogs_are_not_mirrored():
    assert mirror.build_mirror_drop(CLOG, 20997, "Twisted bow", "") is None
    no_guid = {k: v for k, v in CLOG.items() if k != "guid"}
    assert mirror.build_mirror_drop(no_guid, ETERNAL_GLORY, "Amulet of eternal glory", "") is None


def test_submits_through_drop_processor():
    session = MagicMock()
    fake = AsyncMock(return_value="ok")
    with patch("data.submissions.drop.drop_processor", fake):
        out = asyncio.run(mirror.mirror_clog_as_drop(
            CLOG, ETERNAL_GLORY, "Amulet of eternal glory", "", external_session=session))
    assert out == "ok"
    payload = fake.await_args.args[0]
    assert payload["source"] == "Fountain of Rune"
    assert fake.await_args.kwargs == {"external_session": session, "world_type": "main"}


def test_failure_never_raises_and_clears_the_session():
    session = MagicMock()
    with patch("data.submissions.drop.drop_processor", AsyncMock(side_effect=RuntimeError("boom"))):
        out = asyncio.run(mirror.mirror_clog_as_drop(
            CLOG, ETERNAL_GLORY, "Amulet of eternal glory", "", external_session=session))
    assert out is None
    session.rollback.assert_called_once()


def test_clog_processor_mirrors_new_slots_only():
    hook = AsyncMock()
    with patch.object(mirror, "mirror_clog_as_drop", hook):
        # The harness's slot lookup always finds a row, so a multi-slot name is
        # how it records a new slot.
        _run_processor("Chompy bird hat", dedup_query_returns_existing=True)
        assert hook.await_count == 1
        args = hook.await_args.args
        assert args[1] == 2978 and args[2] == "Chompy bird hat"

        hook.reset_mock()
        _run_processor("Twisted bow", dedup_query_returns_existing=True)
        assert hook.await_count == 0
