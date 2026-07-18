"""Long-poll wake hub for ``GET /notifications?wait=N``.

One asyncio task per API process holds a single Redis pub/sub connection
subscribed to the wake channel (``plugin:notify:wake``, published by
``services.plugin_notifications.push_to_inbox`` — the constant is mirrored
here as a literal because this module must stay importable when the unit-test
conftest stubs the ``services`` package). Route handlers register an
``asyncio.Event`` per player_id; the listener sets every waiter for a player
when its id arrives on the channel.

All state lives on the process event loop — handlers and the listener task
share it, so the plain dict needs no locking. Everything is best-effort: if
Redis pub/sub is down, held requests simply wait out their timeout and the
route degrades to the plain-poll behavior.
"""
import asyncio
import os

WAKE_CHANNEL = "plugin:notify:wake"
_RETRY_SECONDS = 2

_waiters: dict = {}


def register(player_id: int) -> asyncio.Event:
    """Add a waiter for this player; caller must unregister() in a finally."""
    event = asyncio.Event()
    _waiters.setdefault(int(player_id), set()).add(event)
    return event


def unregister(player_id: int, event: asyncio.Event) -> None:
    waiters = _waiters.get(int(player_id))
    if not waiters:
        return
    waiters.discard(event)
    if not waiters:
        _waiters.pop(int(player_id), None)


def wake(player_id) -> int:
    """Set every waiter registered for this player. Returns waiters woken."""
    try:
        waiters = _waiters.get(int(player_id))
    except (TypeError, ValueError):
        return 0
    if not waiters:
        return 0
    for event in waiters:
        event.set()
    return len(waiters)


async def run_listener() -> None:
    """Subscribe to the wake channel and dispatch forever; reconnects on error.

    Runs as an app background task (see api/__init__.py before_serving).
    """
    import redis.asyncio as aioredis

    while True:
        client = None
        try:
            client = aioredis.Redis(
                host="127.0.0.1", port=6379, db=0, password=os.getenv("DB_PASS"))
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(WAKE_CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                wake(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[notify_wake] listener error, reconnecting in {_RETRY_SECONDS}s: {e}")
            await asyncio.sleep(_RETRY_SECONDS)
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
