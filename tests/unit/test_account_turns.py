"""utils/account_turns.py: one account's submissions take turns inside a process."""

import asyncio

from utils import account_turns as turns


def _payload(**fields):
    return {"embeds": [{"fields": [{"name": k, "value": v} for k, v in fields.items()]}]}


def test_key_prefers_the_account_hash_in_either_shape():
    assert turns.account_key({"acc_hash": "123", "player_name": "Bob"}) == "h:123"
    assert turns.account_key(_payload(player_name="Bob", acc_hash="-55")) == "h:-55"


def test_key_falls_back_to_the_folded_name():
    assert turns.account_key({"player_name": "Iron_Bob "}) == "n:iron bob"
    assert turns.account_key(_payload(player="IRON  bob")) == "n:iron bob"


def test_no_identity_no_key():
    assert turns.account_key({}) is None
    assert turns.account_key(None) is None
    assert turns.account_key(_payload(type="quest")) is None


def _run_pair(key_a, key_b):
    """Two tasks that each yield inside their turn; returns the event order."""
    order = []

    async def task(name, key):
        async with turns.account_turn(key):
            order.append(f"{name}-in")
            await asyncio.sleep(0.01)
            order.append(f"{name}-out")

    async def main():
        await asyncio.gather(task("a", key_a), task("b", key_b))

    asyncio.run(main())
    return order


def test_same_account_takes_turns():
    # The bug: a quest held its player's row across an await while the
    # level-up from the same quest updated that row and froze the loop.
    assert _run_pair("h:1", "h:1") == ["a-in", "a-out", "b-in", "b-out"]
    assert turns._turns == {}


def test_different_accounts_run_side_by_side():
    assert _run_pair("h:1", "h:2") == ["a-in", "b-in", "a-out", "b-out"]
    assert turns._turns == {}


def test_no_key_never_waits():
    assert _run_pair(None, None) == ["a-in", "b-in", "a-out", "b-out"]


def test_turn_is_released_when_the_body_raises():
    async def main():
        try:
            async with turns.account_turn("h:9"):
                raise ValueError("boom")
        except ValueError:
            pass
        async with turns.account_turn("h:9"):
            return True

    assert asyncio.run(main()) is True
    assert turns._turns == {}
