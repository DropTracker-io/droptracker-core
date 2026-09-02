# Group Export API

Key-authenticated endpoints that let a group pull its own tracked data
programmatically — for spreadsheets, clan websites, competition tooling, etc.

Implementation: `api/routes/group_export.py` (public API service, port 31323).

## Authentication

Every group has an export API key stored in `group_configurations` under
`config_key = 'export_api_key'` (generated at group creation by
`db/group_creation.py`; backfilled for older groups by
`scripts/group_export_keygen.py`). Group admins can view it via the website's
group settings (it is one of the `SENSITIVE_KEYS` in the config registry).

Pass the key with any of:

| Method | Example |
|---|---|
| `Authorization` header | `Authorization: Bearer <key>` |
| `X-API-Key` header | `X-API-Key: <key>` |
| Query parameter | `?api_key=<key>` |

Failures: `401` (no key), `403` (wrong key), `404` (unknown group).

Keys are group-scoped: a key only works for the `group_id` in the URL.

## Common parameters

- `start_time` / `end_time` — ISO-8601 (`2026-07-01T00:00:00Z`) or unix epoch
  (seconds or milliseconds). All timestamps are UTC. Defaults:
  `end_time = now`, `start_time = end_time − 30 days`. Max window: 366 days.
- `npc_id` — a single NPC id or comma-separated list.
- `npc_name` — exact NPC name; automatically matches **all** id variants of
  that name (e.g. every "Dust devil" id). Combinable with `npc_id`.

Notes:

- Hidden players and hidden drops are excluded everywhere.
- Groups with more than 2,500 members (the global catch-all pseudo-groups)
  get `413`; they should use the public global endpoints instead.
- Rate limit: 30 requests/min per endpoint per IP.
- `503` means the query was too heavy to finish in time — narrow the window
  (or add an `npc_id`/`npc_name` filter) and retry. It is safe to retry.

## `GET /groups/<group_id>/export/top-players`

Ranked loot leaderboard for the group's members over the window, optionally
restricted to one NPC (or several), with per-player item breakdowns.

Extra parameters:

- `limit` — players returned (default 25, max 100)
- `include_items` — `true`/`false` (default `true`)
- `items_per_player` — top items per player by value (default 10, max 25)

```bash
curl -H "Authorization: Bearer $KEY" \
  "https://api.droptracker.io/groups/5/export/top-players?npc_id=410&start_time=2026-07-01T00:00:00Z&end_time=2026-07-18T00:00:00Z"
```

```json
{
  "success": true,
  "group": {"group_id": 5, "group_name": "Varietyz"},
  "npcs": [{"npc_id": 410, "npc_name": "Kurask"}],
  "start_time": "2026-07-01T00:00:00Z",
  "end_time": "2026-07-18T00:00:00Z",
  "totals": {"total_value": 1148168, "drop_count": 362, "players_with_drops": 1},
  "players": [
    {
      "rank": 1,
      "player_id": 299,
      "player_name": "DankGoldList",
      "wom_id": 206966,
      "total_value": 1148168,
      "drop_count": 362,
      "first_drop": "2026-07-17T18:13:19Z",
      "last_drop": "2026-07-17T22:00:45Z",
      "items": [
        {"item_id": 5975, "item_name": "Coconut", "quantity": 90,
         "total_value": 147300, "drop_count": 9}
      ]
    }
  ]
}
```

`totals` covers the whole group over the window (not just the returned page).
`npcs` is `null` when no NPC filter was applied (all loot counted).

Without an NPC filter, months before the current one are served from an hourly
rollup, so `start_time`/`end_time` are effectively rounded to the hour for that
older portion and a `first_drop` that falls in it is hour-granular. The current
calendar month is always exact to the second, and day- or month-aligned windows
are exact throughout. NPC-filtered requests are exact to the second everywhere.

## `GET /groups/<group_id>/export/drops`

Raw drop records for the group over the window, newest first.

Extra parameters:

- `player_id` — restrict to one member (404 if not in the group)
- `min_value` — minimum per-drop total value (`value × quantity`)
- `limit` — rows returned (default 100, max 500)
- `offset` — pagination offset (max 100,000)

Each row: `drop_id`, `player_id`, `player_name`, `npc_id`, `npc_name`,
`item_id`, `item_name`, `quantity`, `value_each`, `total_value`,
`date_added`, `received_at`, `image_url`.

`date_added` is ISO-8601 UTC; `received_at` is the same instant as unix
seconds (UTC). Both record when DropTracker **accepted** the submission,
not when the drop occurred in game.

## `GET /groups/<group_id>/export/members`

Current member list: `player_id`, `player_name`, `wom_id`, `total_level`,
`log_slots`, `tracked_since`. Plus `member_count`.
