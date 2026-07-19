# Loot Sweep event kind (`loot_sweep`)

Status: **backend implemented** (2026-07-19) · frontend authoring UI + board
rendering **pending** (this doc is the contract for that work).

A **Loot Sweep** is a race to obtain items from across the game. Unlike a
standard task that *completes once* and awards a flat sum, a Loot Sweep accrues
points continuously and never "completes":

- Each configured **item** awards `points` the first time a team receives it,
  then **decays** on every successive receipt (default −20 percentage-points of
  the base each time → the `100 / 80 / 60 / 40 / 20 %` columns in the authoring
  grid), down to a per-item **cap** (`max_awards`) after which more copies score
  nothing.
- Items belong to a boss/"**set**". When a team has obtained every set item at
  least once, it earns a flat **set bonus** (`set_bonus_points`), repeatable up
  to `set_bonus_max` times (a second full set → the bonus again).

The board the admin drew: one row per item (`item id · pts · boss/item name`),
a per-team column group whose cells fill in as receipts come in, plus a set-bonus
row (`Kree'arra 40`) and an out-of-set bonus row (`pet (bonus) 60`).

---

## Why there are (almost) no new database changes

Loot Sweep reuses the existing events-v2 machinery instead of new tables — the
same decision `board_game` and the `point_collection`/`groups` item-list tasks
made. **The only DB change is the registry row** (already inserted):

```sql
INSERT INTO web_event_types (`key`, label, description, enabled, admin_only, sort)
VALUES ('loot_sweep', 'Loot Sweep',
        'Obtain drops across the game to receive the most points before the event concludes!',
        0, 1, 5);
```

Everything else is modelled on existing tables:

| Concept | Where it lives |
|---|---|
| The event | `web_events` row, `kind = 'loot_sweep'` |
| One boss/"set" | one `web_event_tasks` row, `type = 'loot_sweep'` |
| The set's items + points + caps + set bonus | that task's `config` JSON (below) |
| A team receiving an item | a `web_event_completions` ledger row (`matched_target` = item, `quantity` = count) — idempotent on `(task, team, submission_guid)`, revoke-safe |
| A team's running score for a set | `web_event_progress.progress` (the point total, **not** a threshold count; `completed` stays `false`) |
| Team standings | `web_event_teams.score` |
| Per-player contribution | `web_event_player_points` |

Because the score is a **pure function of the applied ledger** (recomputed each
apply/revoke, like the `all_of`/`groups` rollups), apply and revoke are simple
delta adjustments and can never drift.

> If board reads (the per-item × per-team grid) ever get too heavy off the
> ledger, a denormalized `web_event_loot_progress(task_id, team_id, item_key,
> count, points)` counter table is the natural optimization — but it is **not**
> needed for correctness and is deliberately not built yet.

---

## The task `config` shape (`kind: "loot_sweep"`)

One task = one set. The write path
(`web_api/routes/event_task_validation.py::_validated_loot_sweep`) validates and
normalizes it; the engine + `services/loot_sweep.py` read the normalized form:

```jsonc
{
  "kind": "loot_sweep",
  "decay_percent": 20,          // 0-100; points shed per successive receipt
  "decay_mode": "linear",       // "linear" | "geometric" (default linear)
  "default_max_awards": 5,      // per-item cap unless the item overrides it
  "set_bonus_points": 40,       // flat bonus for a full set (0 = no set bonus)
  "set_bonus_max": 1,           // times the set bonus pays out per team
  "items": [
    { "item_id": 11826, "item_name": "Armadyl helmet",     "points": 9 },
    { "item_id": 11828, "item_name": "Armadyl chestplate",  "points": 9 },
    { "item_id": 11830, "item_name": "Armadyl chainskirt",  "points": 9 },
    { "item_id": 11810, "item_name": "Armadyl hilt",        "points": 13 },
    { "item_id": 22473, "item_name": "Pet kree'arra",       "points": 60,
      "counts_for_set": false }          // scores, but never gates set completion
  ]
}
```

Per-item keys:

| Key | Meaning | Default |
|---|---|---|
| `item_name` | **Exact** in-game name (validated against `item_list`; snapped to canonical spelling). | required |
| `item_id` | Game id, resolved server-side for icons — the frontend does not need to send it. | resolved |
| `points` | Base points for the **first** receipt. | 1 |
| `max_awards` | Per-item cap on scoring receipts. | `default_max_awards` |
| `counts_for_set` | Whether the item is required for set completion. Set `false` for extras like pets. | `true` |

**Validation bounds** (`event_task_validation.py`, mirrored in
`services/loot_sweep.py`): ≤ 100 items/task, `points` 1–1,000,000, `max_awards`
1–100, `set_bonus_points` 0–10,000,000, `set_bonus_max` 1–100. Unknown item
names are rejected `422` with the offending names listed. `target` /
`target_value` are unused for this type.

### The decay math

k-th receipt (1-indexed) multiplier, then `round()`-ed and summed:

- **linear** (default): `max(0, 1 − (k−1)·decay/100)` → for `decay=20`:
  `1.0, 0.8, 0.6, 0.4, 0.2, 0` — matches the grid columns exactly.
- **geometric**: `(1 − decay/100)^(k−1)` → for `decay=20`:
  `1.0, 0.8, 0.64, 0.512, …`

So `base 40, decay 20, linear` over 5 receipts = `40+32+24+16+8 = 120`.

### Set completion

`sets_completed = min(receipt count over every item where counts_for_set)` (0 if
the set has no such items or `set_bonus_points = 0`). Payout =
`set_bonus_points × min(sets_completed, set_bonus_max)`. A team that collects two
full sets with `set_bonus_max = 1` still gets the bonus once.

**Standalone items** (no set): just give the task `set_bonus_points: 0`. It still
scores every item with decay + caps; there is simply no bonus row.

---

## Engine wiring (already done)

All in `services/event_engine.py` unless noted:

- `AUTO_TASK_TYPES` includes `loot_sweep` → its tasks load into the matcher.
- `match_task()` — a `loot_sweep` branch credits any config item obtained via a
  `drop` or `clog`, folding raw quantity; scoring happens at apply time.
- The drop↔clog echo dedupe (`_dedupe_clog_echo`) now covers `loot_sweep` too,
  so a unique that arrives as both a drop and a collection-log unlock is counted
  once.
- `apply_ledger_row()` → `_apply_loot_sweep()`: recompute the team's running
  total off the ledger, fold the **delta** into `EventTeam.score`, store the
  total in `EventProgress.progress`, re-split contribution points, publish SSE,
  fan out the in-game plugin note, and — only when a **full set just completed**
  — enqueue a Discord `event_completion`; also fires `event_lead_change`.
- `revoke_ledger_row()` → `_revoke_loot_sweep()`: same recompute, delta-reversed.
- `_row_advances_progress()` drops dead-weight receipts (item capped **and** no
  new set) before they hit the ledger, so capped items stop spamming popups.
- `pending_projection()` returns applied/projected **point totals** for the
  pending-review overlay (`pending_complete` is always `false`).
- Scoring itself is the pure, unit-tested `services/loot_sweep.py`
  (`tests/unit/test_loot_sweep.py`).

Constants registering the kind/type: `db/models/events.py` (`EVENT_KINDS`,
`EVENT_TASK_TYPES`) and the web contract `packages/api-types/src/index.ts`
(same two arrays — **both repos must list it or the admin page 500s**, which is
what the unknown `loot_sweep` key originally did to the Zod `.parse()`).

---

## Read endpoint (the live board)

`GET /api/v1/events/{id}/loot-sweep` (public, viewer-permission aware) returns
every `loot_sweep` set with, per team, the per-item receipt counts + decayed
points + set-bonus status — rebuilt from the applied ledger via
`services/loot_sweep.py::score_counts`, so it can never disagree with
`EventTeam.score`. `sets[].items` (config order) carries the item defs; each
`sets[].teams[].items` is the SAME-INDEXED `{count, scored, points}` so the grid
maps by position. Implemented in `web_api/routes/events.py::get_loot_sweep_board`.

## SSE / realtime frames

`rt:event:{id}` gains a `loot_sweep` frame on each scoring receipt:

```jsonc
{ "kind": "loot_sweep", "event_id", "task_id", "team_id", "player_id",
  "player_name", "task_label", "received_item": "Armadyl hilt",
  "points": 13,          // the delta this receipt added
  "total": 53,           // the team's running total for this set
  "team_score": 512,     // team's overall event score
  "set_completed": true } // a full set just paid out
```

Revokes publish `{ "kind": "revoke", …, "loot_sweep": true, "progress": <total> }`.

---

## Frontend (built — droptracker-web)

The web UI is implemented (see the web repo `docs/loot-sweep-frontend.md`):

1. **Admin toggle card** — `/admin/event-types` renders the Loot Sweep card
   (Enabled / Staff-testing-only / test-groups) like any kind.
2. **Create-form kind option** — the kind picker reads `GET /events/meta/types`
   (registry-driven), so Loot Sweep appears automatically once creatable.
3. **Authoring editor** — `components/loot-sweep-editor.tsx`: item search +
   per-item points / `max_awards` / `counts_for_set` with a live decay preview,
   and the set-level `set_bonus_points` / `set_bonus_max` / `decay_percent` /
   `decay_mode` controls. Wired into `event-task-form.tsx` for
   `type: "loot_sweep"`.
4. **Live board** — `components/loot-sweep-board.tsx`: an icon-first "collection
   race" (greyed-out until obtained, ×count + scored/cap pips once received, set
   badge + running total), fed by the read endpoint above and refetched on SSE.

**Still a backend follow-up:** `services/event_board_image.py` has no
`loot_sweep` signature, so the Discord board *image* for this kind isn't drawn
yet; and the per-set-completion Discord message reuses the `event_completion`
layout with `loot_sweep: true` + `points_based: true` markers — a
Loot-Sweep-aware layout is later polish (per-receipt detail already rides SSE +
the plugin inbox).

## Open product questions (sensible defaults chosen; easy to change)

- **A "receipt" counts stack quantity** (a stack of 3 = 3 decaying receipts,
  capped at `max_awards`). Fine for uniques; revisit if stackables are added.
- **Decay defaults to linear** to match the grid. `geometric` is supported.
- **Discord announces only full-set completions** to avoid per-drop spam. Make
  it configurable if groups want per-receipt posts.
