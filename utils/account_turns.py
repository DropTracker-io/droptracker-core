"""One submission at a time per account, inside one process.

The intake processors run on an asyncio loop but talk to MySQL synchronously,
and a processor keeps its transaction open across awaits (the Discord-webhook
screenshot download, ``create_notification``, plain ``asyncio.sleep(0)``
yields). Inserting any row that references ``players`` (quest_completions,
notification_queue, drops, ...) takes a shared lock on that player's row until
the commit. If a second submission for the same player runs during one of
those awaits and writes the row itself (``UPDATE players SET total_level``
from a level-up), its blocking statement freezes the whole loop, so the
first task can never reach its commit: the update waits out
innodb_lock_wait_timeout and fails, and every other submission in the
process stalls with it.

A quest completion and the level-up its XP reward causes arrive together, so
this hit new accounts every day (bots/webhook_bot.py, 2026-11-02 onward: a
quest notification at the exact second of every timeout). Taking turns per
account removes the only writer that can contend for a player's row inside
the process. Different accounts still run side by side.

Stdlib only at import.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

#: account key -> [lock, number of holders and waiters]
_turns: dict = {}


def account_key(data) -> Optional[str]:
    """The account a submission belongs to: its account hash, else its player
    name (case and spacing folded). ``data`` is a flattened embed (field name
    -> value) or a raw webhook payload with ``embeds[].fields[]``. None when
    neither is present."""
    if not isinstance(data, dict):
        return None
    candidates = [data]
    for embed in data.get("embeds") or ():
        if isinstance(embed, dict):
            candidates.append({
                f.get("name"): f.get("value")
                for f in embed.get("fields") or () if isinstance(f, dict)
            })
    for fields in candidates:
        acc_hash = str(fields.get("acc_hash") or "").strip()
        if acc_hash:
            return f"h:{acc_hash}"
    for fields in candidates:
        name = fields.get("player_name") or fields.get("player")
        name = " ".join(str(name or "").lower().replace("_", " ").split())
        if name:
            return f"n:{name}"
    return None


@contextlib.asynccontextmanager
async def account_turn(key: Optional[str]):
    """Wait for this account's turn. A None key runs at once."""
    if not key:
        yield
        return
    entry = _turns.get(key)
    if entry is None:
        entry = _turns[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0 and _turns.get(key) is entry:
            del _turns[key]
