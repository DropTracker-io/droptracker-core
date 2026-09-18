"""Platform-wide figures for the public homepage.

  GET /api/v1/platform/summary   (public, cached 30s)

    {
      "partition": 202609,
      "generated_at": 1789737906,
      "monthly_loot": { "value": 287816175687, "value_formatted": "287.82B" } | null,
      "member_count": 27856 | null,
      "top_bosses": [ { "npc_id", "name", "loot": {money}, "drops" } ],
      "snapshot_at": 1789737600 | null
    }

``monthly_loot`` is read live from Redis on every request. ``member_count`` and
``top_bosses`` come from a Redis-held snapshot that is rebuilt behind the
response once it is ten minutes old; they are null / empty only while the very
first snapshot of a month is being built. See ``web_api/platform_summary.py``
for where each number comes from and why this is not ``GET /groups/2``.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from web_api.common import with_cache_headers

platform_bp = Blueprint("v1_platform", __name__)

# The event loop only holds weak references to tasks; without this a rebuild
# could be collected mid-flight.
_refreshing: set = set()


async def _refresh_behind(partition: int) -> None:
    try:
        from web_api.platform_summary import refresh

        await asyncio.to_thread(refresh, partition)
    except Exception as e:  # never surface as an un-retrieved task exception
        print(f"platform_summary: background refresh failed: {type(e).__name__}: {e}")


@platform_bp.get("/platform/summary")
async def platform_summary():
    from web_api.platform_summary import load

    payload, stale = await asyncio.to_thread(load)
    if stale:
        task = asyncio.get_running_loop().create_task(_refresh_behind(payload["partition"]))
        _refreshing.add(task)
        task.add_done_callback(_refreshing.discard)
    return with_cache_headers(jsonify(payload), max_age=30)
