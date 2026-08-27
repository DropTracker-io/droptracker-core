# DropTracker Data API (v2)

Authenticated read access to player and group data, for third-party apps.

Base URL: `https://api.droptracker.io/v2`
Dev: `http://dev-api.droptracker.io/v2`

This is a separate service (`droptracker-data-api`, port 31326) from the
RuneLite intake API and the website backend. That isolation is deliberate: a
slow or heavy query here cannot delay a submission or a page load. The legacy
group export (`/groups/{id}/export/...`, `docs/GROUP_EXPORT_API.md`) is
unchanged and remains available; it is not deprecated by this document, but new
integrations should use v2.

---

## Authentication

Every endpoint except `/v2/health` requires a bearer token:

```
Authorization: Bearer dtk_<key_id>_<secret>
```

There is no query-parameter form. A key in a URL ends up in access logs,
browser history and `Referer` headers, so the header is the only accepted
carrier.

Keys are shown **once**, at creation, and stored only as a SHA-256 digest — we
cannot recover one for you. Lose it and you rotate it.

A missing, malformed, unknown, revoked or expired key all produce the same
`401`. This is intentional: the id embedded in a token must not become a way to
probe which keys exist.

### Getting a key

| Owner | How | Who may |
|---|---|---|
| A user | `POST /api/v1/me/api-keys` on the website | Supporters |
| A group | `POST /api/v1/groups/{id}/api-keys` | Group owner/admin |
| Either | Ask staff | Custom limits, any tier |

Maximum 5 active keys per owner. Revoking is immediate.

### Scope

A key sees only what its owner could see on the website, and no more:

* a **group** key reads that group's members;
* a **user** key reads the accounts that user has claimed.

A player who is hidden — or whose account owner is hidden — is invisible here
exactly as they are on droptracker.io, and returns `404`. A player outside your
scope returns `404` as well, not `403`: distinguishing the two would let you
enumerate other clans' rosters.

Group `2` is an internal pseudo-group containing every player and cannot be
exported.

---

## Rate limits

Limits attach to the **key**, not to any subscription. Every key — including
those owned by premium groups — starts on the lowest tier. Higher tiers are
granted by staff once a consumer's traffic has proven well-behaved, so if you
need more, get something working first and then ask.

Four budgets apply:

| Budget | Meaning |
|---|---|
| `requests_per_min` | Burst ceiling |
| `cost_units_per_min` | Actual work performed (see below) |
| `requests_per_day` | Sustained volume |
| `max_concurrency` | Requests in flight at once |

`GET /v2/meta` reports your key's current limits. Read them at runtime rather
than hardcoding — they change when a key is promoted.

Responses carry:

```
X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset
X-RateLimit-Cost, X-RateLimit-Cost-Limit, X-RateLimit-Cost-Remaining
```

Exceeding a budget returns `429` with `Retry-After` and a `limit` field naming
which budget was hit.

### The cost model

Not all requests are equal: one player's headline loot total is a Redis read,
while a hundred players' full collection logs is a six-figure row count. So
requests are priced:

```
cost = number of players x sum of the cost of each requested section
```

**Cost is charged before the query runs.** An over-budget request is refused
rather than executed and billed afterwards — which is what keeps one integration
from degrading the service for everyone.

`GET /v2/sections` lists every section with its per-player cost, so you can
price a call before making it.

---

## Endpoints

### `GET /v2/health`
Liveness. No authentication.

### `GET /v2/meta`
Your key: id, label, tier, owner scope and effective limits.

### `GET /v2/sections`
Every available section, its cost, and what it contains.

### `GET /v2/players/{id_or_name}`
One player. `{id_or_name}` is a numeric player id or an exact RSN.

```
GET /v2/players/12345?include=identity,stats,loot,personal_bests
```

### `GET /v2/groups/{group_id}/players`
A page of the group's roster, each player carrying the same sections.

| Parameter | Default | Notes |
|---|---|---|
| `include` | `identity` | Comma-separated section list, or `all` |
| `limit` | 25 | Max 100 players per page |
| `cursor` | — | `next_cursor` from the previous response |
| `days` | 30 | Window for the loot sections, max 366 |
| `top` | 10 | Rows per player in `loot_npcs` / `loot_items`, max 50 |

Pagination is by cursor, not offset:

```
GET /v2/groups/7/players?include=identity,loot&limit=100
  -> { "count": 100, "next_cursor": 88213, "players": [...] }
GET /v2/groups/7/players?include=identity,loot&limit=100&cursor=88213
```

`next_cursor` is `null` on the last page.

---

## Sections

| Section | Cost | Contains |
|---|---|---|
| `identity` | 0 | Name, account type, combat/total level, EHB, last sync. Always included. |
| `loot` | 1 | Month and all-time GP — the same numbers as the leaderboard |
| `stats` | 2 | Experience in all 24 skills, and the total |
| `clog` | 2 | Collection log progress (obtained / total) |
| `combat_achievements` | 2 | Tasks completed and points |
| `quests` | 2 | Counts by state |
| `diaries` | 2 | Completion counts per area and tier |
| `points` | 2 | Lifetime points earned |
| `badges` | 2 | Currently held badges |
| `pets` | 2 | Pets received |
| `deaths` | 2 | Death count and most recent |
| `personal_bests` | 3 | Best time per boss and team-size bracket |
| `loot_npcs` | 5 | Top NPCs by loot value over the window |
| `loot_items` | 5 | Top items by loot value over the window |
| `clog_slots` | 8 | Every recorded collection log slot (~1,500 rows per player) |

Requesting an unknown section is a `400` naming it, rather than a response
quietly missing the data you asked for.

If one section fails while others succeed, that section comes back as
`{"error": "unavailable"}` and the rest of the response is still served.

---

## Errors

| Status | Meaning |
|---|---|
| `400` | Unknown section or malformed parameter |
| `401` | Missing, invalid, revoked or expired key |
| `403` | Valid key, but not scoped to that group |
| `404` | No such player, or outside your scope, or hidden |
| `429` | A rate-limit budget was exceeded — see `Retry-After` |
| `503` | The query exceeded the server's time limit — narrow `days` or request fewer sections |

`503` is not a crash: the server enforces a hard query ceiling and gives up
cleanly rather than holding a connection open. Retrying the same request
unchanged will usually produce the same result; make it smaller.

---

## Notes on the data

* **Loot totals come from the same source as the leaderboard**, so the two can
  never disagree.
* `loot_npcs` and `loot_items` are served from hourly rollups. They aggregate
  over whole hours, so a drop from a few minutes ago may not appear instantly.
* Collection log `obtained`/`total` is the game's own counter, which knows
  about slots we hold no row for. `tracked_items` is our row count and will
  usually be lower — that is expected, not a discrepancy.
* Timestamps are UTC ISO-8601.
* `first_seen_at` on a collection log slot is when *we* recorded it, never when
  the player obtained it.
