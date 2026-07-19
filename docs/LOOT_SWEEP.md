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

## The task `config` shape (`kind: "loot_sweep"`) — v2 (nested groups)

One task = one "set" of one or more **groups** (sub-sets). A group ties items to
their source NPC(s) and awards `bonus_points` when a team collects all its
gating items once; the task awards `set_bonus_points` when **every** group is
complete. A simple boss is one group; a meta-set (Barrows) is many. The write
path (`web_api/routes/event_task_validation.py::_validated_loot_sweep`) validates
and normalizes; the engine + `services/loot_sweep.py` read the normalized form:

```jsonc
{
  "kind": "loot_sweep",
  "decay_percent": 20,          // 0-100; points shed per decay TIER
  "decay_mode": "linear",       // "linear" | "geometric" (default linear)
  "set_bonus_points": 40,       // awarded once EVERY group is complete (0 = none)
  "set_bonus_max": 1,           // times the whole-set bonus pays out per team
  "groups": [
    { "label": "Ahrim",
      "npcs": ["Ahrim the Blighted"],   // a drop only counts from these NPC(s)
      "bonus_points": 4,                // awarded when all this group's set-items collected once
      "bonus_max": 1,
      "items": [
        { "item_name": "Ahrim's hood", "points": 1 },
        { "item_name": "Ahrim's staff", "points": 1 },
        …
      ] },
    …  // 6 brothers → set_bonus_points 40 when all six are done
  ]
}
// Batched decay (Brimstone: full points for 3, then the 20% step for 3, …):
//   { "item_name": "Brimstone key", "points": 4, "awards_per_tier": 3 }
// Scores but doesn't gate its group's bonus (pets, mega-rares):
//   { "item_name": "Pet kree'arra", "points": 60, "counts_for_group": false }
```

Per-item keys:

| Key | Meaning | Default |
|---|---|---|
| `item_name` | **Exact** in-game name (validated against `items`; snapped to canonical). | required |
| `item_id` | Game id, resolved server-side for icons — the frontend needn't send it. | resolved |
| `points` | Base points for the **first** receipt. | 1 |
| `awards_per_tier` | Receipts sharing each decay tier — full points for this many before the first 20% step. | 1 |
| `max_awards` | Total scoring receipts. | `5 × awards_per_tier` |
| `counts_for_group` | Whether the item gates its group's bonus. `false` for pets/mega-rares. | `true` |

Per-group keys: `label`, `npcs` (list; validated against `NpcList`), `bonus_points`,
`bonus_max`. **An item may appear in only one group per task** (so a receipt maps
to one group) — enforced `422`.

**Validation bounds**: ≤ 40 groups, ≤ 400 items/task, `points` 1–1,000,000,
`awards_per_tier` 1–20, `max_awards` 1–200, bonuses 0–10,000,000 / max 1–100.
Unknown item/NPC names are rejected `422`. `target`/`target_value` are unused.
(v1 flat `{items:[…]}` configs still parse — wrapped into a single group.)

### Decay math (batched)

Receipts are grouped into decay **tiers** of `awards_per_tier` each. The tier of
receipt `k` is `(k−1) // awards_per_tier`; its multiplier (rounded per receipt,
summed) is `max(0, 1 − tier·decay/100)` (linear) or `(1 − decay/100)^tier`
(geometric). So `awards_per_tier = 1, decay = 20` gives `1.0, 0.8, 0.6, 0.4, 0.2`
(then 0); `awards_per_tier = 3` gives full points for receipts 1-3, `0.8` for
4-6, etc. — the sheet's duplicate rows.

### Group + set completion

- **Group** `g_completions = min(count over g's counts_for_group items)`; payout
  `g.bonus_points × min(g_completions, g.bonus_max)`.
- **Set** `set_completions = min(g_completions over gating groups)` (a group is
  gating when it has completion items); payout
  `set_bonus_points × min(set_completions, set_bonus_max)`.

A **standalone boss** is just one group with `set_bonus_points: 0` — its group
bonus is the boss bonus.

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
