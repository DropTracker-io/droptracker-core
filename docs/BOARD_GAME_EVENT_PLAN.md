# Board-Game Event Type — Full-Stack Implementation Plan

**Status:** SHIPPED 2026-07-15 — P0–P3 core LIVE in production (migrations web43a/web44a/web45a applied; all phases E2E-verified against the live API; both repos committed + pushed + deployed). `board_game` is enabled + admin_only (staff testing) in /admin/event-types.

**Deferred polish (tracked, not blocking):** rotating-shop turn-window editor UI (schema + filtering exist), Discord Activity board parity, admin turn-rewind tooling, custom piece-image upload, roadblock visibility choice (currently hidden traps).

Author: investigation across events v2, legacy board-game archive, points systems, admin/entitlements, item/asset infra, and the bingo designer.

This plan covers two features that ship together:

1. **Event types** — a first-class "type/kind" dimension on events, each type toggleable site-wide from the superadmin dashboard. Disabled types are creation-gated (see decision D6).
2. **Board-game event type** — an admin-only-to-start, feature-rich game mode: a linear tile board with per-team game pieces, a **dice-driven turn loop**, a coin economy, and a rotating power-up shop, plus an image-based board designer that reuses the bingo designer's autosave.

### Resolved product decisions (from review)
- **Scope:** build the **full P0→P3 arc**, delivered in sequenced phases.
- **Movement (revised):** a turn is **start task → complete task → roll dice → move X tiles forward → land on a new tile** (X = dice result). Dice are the mechanism, but **everything is configurable per event** (see below). Win = **first team to the finish tile** (tiebreak coins → score).
- **Tiles carry a difficulty by default, not a specific task.** A tile primarily holds a **difficulty tier**; on landing it **rolls a random task from the event's own task pool of that difficulty**, so different teams (and re-landings) get different tasks and boards are fast to author. Pinning a specific task to a tile is an optional override. A designer **"auto-set tile type"** button cycles air→water→earth→fire across all tiles (confirm before overwriting; still editable after).
- **Configurability is a first-class, day-one requirement.** Group leaders get full control from the start: dice count/sides (or a fixed-step fallback), **auto-roll vs. manual-prompt** (and *who* may trigger the roll — team member vs. group admin), the whole **shop on/off**, **individual shop items on/off**, power-up categories, mercy rule, coins-per-difficulty, win rule, and the board's **visual tile rendering**. Assume "make it as customizable as possible" for every mechanic below.
- **Disabled-type creation gate:** **superadmin + a per-type test-group allowlist** — superadmins everywhere, plus admins of explicitly allowlisted groups may create a disabled/admin-only type for testing.
- **Board designer tile rendering (configurable):** each placed tile can render as (a) the **elemental rune icon** matching the tile's task difficulty (air/water/earth/fire), (b) **invisible** (rely on the background art), or (c) an **outline** with configurable thickness/color. Chosen per board so leaders whose background already depicts tiles can hide the overlay.

---

## 0. Grounding — what already exists (and what to reuse)

### Events v2 today (the substrate)
- Namespaced `web_*` tables in `db/models/events.py`. Core: `Event`, `EventTask`, `EventTeam`, `EventTeamMember`, `EventProgress` (rollup), `EventCompletion` (ledger), `EventBingoCell/Completion`, `EventTaskLibraryItem`, `EventTemplate`, `EventChannel`, `EventGuild`, `EventMessageLayout`.
- **Submission-driven async engine**, not turn-based: processors call `event_engine.queue_submission()` → Redis `events:submissions` → `workers/event_consumer.py` → `event_engine.handle_envelope()`. See `services/event_engine.py`.
- **Critical structural fact:** `handle_envelope()` (`event_engine.py:1416`) evaluates **every** task on an event against each submission (bingo's "all cells always live" model). There is **no** per-team "current task", task ordering, or turn concept anywhere (confirmed: `EventTask` has no `order` column; the only `idx` is `EventBingoCell.idx`).
- Completion (`apply_ledger_row`, `event_engine.py:1199-1321`): when `progress >= completion_threshold`, sets `completed=True`, does `team.score += points` (`:1272`), fires SSE + `event_completion` notification. `EventTeam.score` is a **running total, never recomputed** — every write is `+= / -=` at known sites (`:1272`, `:1008`, `:1534`, `:1568`, `:1621`).
- Lifecycle (`services/event_lifecycle.py`): `draft → active → past`. `activate_event()` (`:410`) seeds teams/rosters/free-cells but **creates no progress rows** — those are lazy. This is where board-game per-team turn state must be initialized.
- Matcher state (`load_matcher_state`, `event_engine.py:607`) is loaded query-free and refreshed every 30 s / on `rt:event-admin` bump / after lifecycle transitions (`event_consumer.py`).

### The legacy board game (prior art — the design reference)
Deleted on-disk (2026-07-06 audit), preserved in `docs/archive/legacy-events/legacy-events-code_20260705.tgz` + `events_legacy_20260705.sql`. Two implementations:

- **`BoardGame.py`** (DB-backed, config/persistence reference; item effects were **stubbed**): 4-element tiles AIR/WATER/EARTH/FIRE == difficulty tiers; dice roll → move → land on tile → draw a task of that element; a team can't roll again while it has an incomplete task; teams start with 5 gold; win at 100 points; **mercy rule** — a team's task auto-completes after ~24 h (+12 h × mercy_count) so unobtainable RNG can't freeze a team.
- **`GielinorRace.py`** (Redis-backed, **item effects fully implemented** — the mechanics reference): 100-tile board; a **realized shop** — *Teleport* (50g, reroll task to easier tier / jump), *Protection* (30g, +2 next roll / shield next negative), *Boost* (40g, 2× coins next task); **offensive items** (OFFENSIVE/DEFENSIVE/SPECIAL taxonomy) with a % chance to fire on an opponent's turn applying `lower_roll` / `move_backwards`; completing a task = forward move + 50% chance to drop a random shop item; points = difficulty × 10.
- Legacy SQL tables to mine as a schema blueprint: `event_items` (shop catalog: name/description/cooldown/cost/effect/emoji/item_type/effect_long — **designed, never populated**), `event_team_inventory`, `event_team_cooldowns`, `event_team_effects` (where `lower_roll`/`move_backwards` effects persist), `event_teams` (carries `current_location`, `gold`, `points`, `mercy_rule`, `mercy_count`, `current_task`).
- `EventType` enum in the archive already enumerated `BOARD_GAME, BINGO, BOSS_HUNT`.

**How your brief differs from legacy (and why it's cleaner):** you define a **turn = start task → complete → roll a new task** — i.e. advancement is driven by *task completion*, not dice. This fits the submission-driven engine far better than the legacy manual `/event roll` loop: progress streams in automatically against the team's one active task, and completing it advances the piece. No dice loop required (see D1).

### The task library / difficulty (already board-game-aware)
- `EventTaskLibraryItem.difficulty` (`events.py:442`) carries air/water/earth/fire. It is validated first-class in the admin API (`event_admin.py:984`) but **deliberately hidden from the bingo UI** — both pickers comment that it's reserved "until a board-style event type returns" (`event-bingo-designer.tsx:669`, `event-task-library-picker.tsx:182`). **This is that event type.**
- Current DB: 169 library rows (140 `legacy_v1` + 29 group). By difficulty: air 52, water 63, earth 23, fire 1, NULL 30. **Caveat:** `scripts/cleanup_event_task_library.py` converted many legacy presets to `type=custom` because their "item names" were categories ("Points", "Uniques", "Godsword") that aren't real OSRS items — those are manual-award goals, not auto-trackable. The `fire` tier is nearly empty; the board pool will need seeding/rebalancing.
- Task `config.kind` vocabulary (`all_of` / `any_of` / `assembly` / `point_collection`) descends directly from the four legacy task semantics.

### Points / economy (the ledger to mirror for coins)
Two systems exist:
- **Group points** (`db/models/group_points.py`, `data/submissions/point_awards.py`, `web_api/routes/points.py`, `components/points-manager.tsx`) — append-only ledger, `balance = SUM(amount)`, **no spend**. Its value here is the **admin-UI template**: `points-manager.tsx`'s section-per-concern layout (rules/overrides/lists/timed-boosts/seasons, each self-contained with its own state + API + audit) is exactly the shape for a shop-management page.
- **Premium points** (`db/models/premium_features.py`, `services/points.py`) — the **only spendable ledger in the app**: `PointCredit` (with `amount_remaining`) + `PointDebit` (with `allocations` JSON) + FIFO `_consume_points()` + `PremiumFeature` catalog + `FeatureActivation` inventory + atomic `activate_feature_for_group()` (all in one `session.begin()`). This is the reference pattern for coins → shop → inventory → purchase.
- **Difficulty is never used to scale points today** — the only tier→points precedent is the hardcoded CA ladder (`easy_ca`=1 … `grandmaster_ca`=6, `point_awards.py:1163`). A board-game difficulty→coins map follows that precedent.

### Admin / feature-toggle infra
- **No global feature-flag table exists.** The precedent for a site-wide switch is the **seasonal kill switch**: a Redis key (`seasonal:active`) via `services/seasonal_state.py`, `GET/POST /admin/seasonal` (`web_api/routes/admin.py:302`, superadmin-gated + audited), `lib/api.ts:2439`, and `components/admin/seasonal-toggle-panel.tsx` on `/admin/services`.
- **Role resolution** in `web_api/deps.py`: `is_superadmin` (`:215`), `assert_superadmin` (`:219`); superadmins bypass entitlements.
- **Event-creation chokepoint:** `_assert_event_admin()` (`web_api/routes/events.py:943`) called from `create_event()` (`:1034`). This is the single gate for the type-enablement check.
- **Admin IA** is defined once in `web/apps/web/lib/admin-nav.ts` (`ADMIN_SECTIONS`); the Events section already holds `/admin/events` + `/admin/task-library`.

### Item icons & image uploads (assets)
- Item/NPC models carry **only id + name** — no icon column. Icons are a **URL convention**: `https://www.droptracker.io/img/{itemdb|npcdb}/{id}.png` (28,775 item PNGs + 1,067 npc PNGs on the local static tree at `static/assets/img/`, served off :8080). Frontend primitive `ItemDbIcon` (`components/item-db-icon.tsx`); backend resolver `web_api/task_tiles.py` (`COINS_ITEM_ID = 1004` is the default coin icon).
- **Reusable picker:** `ItemNpcPicker` (`components/item-npc-picker.tsx`) — name-keyed search yielding `{name, id}`, backed by `GET /events/meta/{items,npcs,resolve}`. Directly reusable for shop-item icons and item-based team pieces (**no upload needed** if a piece is an OSRS item).
- **Two upload patterns:** (A) group-icon local-tree — `POST /groups/{id}/icon` (`group_admin.py:554`), PIL re-encode, content-hashed file under `static/assets/img/`, `*_icon_url` column; (B) B2 server-side — `POST /uploads/proof` → `utils/b2_storage.py upload_bytes` (`dt_` key prefix, CDN URL). `static/assets/img/events/` + `.../cover/` dirs already exist. Use (A) for small custom team pieces; (B) for large user-uploaded **board backgrounds**.
- **Teams have `color` only, no icon** (`EventTeam`, `events.py:204`). Adding a game piece needs a new column.

### The bingo designer (the autosave pattern to clone)
- `components/event-bingo-designer.tsx` (792 lines). Autosave = **debounce (1500 ms) + revision-guard + serialized single-flight PUT + 30 s periodic sweep + immediate flush on editor close + beforeunload guard** (refs at `:144-157`, `schedule/markDirty/flush` at `:161-246`). A new designer **reuses this block wholesale**; only `buildInput()` and the cell-editor UI change.
- Round trip: edit → `markDirty` → debounce → `flush` → server action `saveEventBingo` (`groups/[id]/events/actions.ts:376`) → `api.saveEventBingo` (`lib/api.ts:811`) → `PUT /events/{id}/bingo` (`event_admin.py:693`, replaces whole board, GCs orphan auto-tasks, flips `has_bingo`/`board_size`, returns refreshed detail). Browser → BFF only (no direct :31325).
- Mount point: the "Board" section in `components/event-manager.tsx:934`.

---

## 1. Feature A — Event types + admin enable/disable

Small, foundational, ship first. Unblocks the board-game type and future types (boss hunt, etc.).

### Data model
- **`Event.kind`** — new `String(24)` column, default `"standard"`, `server_default`. Values: `standard`, `bingo`, `board_game`. (`mode` = ownership shape standard/clan_vs_clan stays orthogonal; do **not** overload it. Note: the create form currently mislabels `mode` as "Event type" — relabel it "Ownership".) Backfill: `kind = 'bingo'` where `has_bingo=1`, else `standard`.
- **`web_event_types`** — the registry + toggle store (durable; the seasonal Redis pattern is fine for a single boolean but a type *list* wants a table):

  | column | type | notes |
  |---|---|---|
  | `key` | PK String(24) | `standard`, `bingo`, `board_game` |
  | `label` | String(48) | display name |
  | `description` | Text | admin help text |
  | `enabled` | Boolean | site-wide on/off |
  | `admin_only` | Boolean | if true, only superadmins (or allowlisted test groups) may create, independent of `enabled` |
  | `sort` | Integer | ordering in pickers |
  | `min_entitlement` | String(24) nullable | e.g. still require `events` entitlement |

  Seed: `standard`/`bingo` enabled + not admin_only; `board_game` enabled + `admin_only=true` (launches admin-only per your brief).
- **`web_event_type_test_groups`** — the per-type allowlist for the resolved gate: `type_key` (FK), `group_id` (FK). A group in this list may create the type even while it's disabled/admin_only (its admins get to test). Managed from the `/admin/event-types` page.
- Cache the registry in Redis (mirror `seasonal_state.py`'s 30 s in-process cache) so the create hot-path and the matcher stay query-light; bust on admin write via the existing `publish_event_admin_bump()`.

### Backend
- New `services/event_types.py`: `list_event_types()`, `is_event_type_creatable(kind, *, is_superadmin, group_id, test_group_ids) -> bool`, cache + invalidation. **Resolved gate logic:**
  ```
  if is_superadmin:                 return True          # superadmin everywhere
  t = registry[kind]
  if not t.enabled:                 return group_id in t.test_group_ids   # disabled → allowlisted test groups only
  if t.admin_only:                  return group_id in t.test_group_ids   # admin-only → allowlisted test groups only
  return True                                            # enabled + public → normal (still needs events entitlement + group-admin)
  ```
- Admin API in `web_api/routes/admin.py` beside `/admin/seasonal`: `GET /admin/event-types`, `PATCH /admin/event-types/{key}` (`enabled`, `admin_only`), and `POST/DELETE /admin/event-types/{key}/test-groups` (manage the allowlist) — `_require_superadmin` + `_audit("event_type.toggle", ...)`.
- **Creation gate** in `create_event()` (`events.py`, right after `mode` validation ~`:1010`, before/with `_assert_event_admin` at `:1034`): validate `kind` ∈ registry; then `if not is_event_type_creatable(...): abort 403`. Creation-only, so existing events keep running untouched.

### Frontend
- `event-create-form.tsx`: add a real **type** selector (populated from `GET /admin/event-types` or a public subset); hide/disable types the current user can't create; relabel the existing `mode` control "Ownership".
- New admin page `admin/event-types/page.tsx` + one entry in `lib/admin-nav.ts` Events section; toggle UI modeled on `seasonal-toggle-panel.tsx`. BFF methods in `lib/api.ts` beside `adminSeasonal`.
- New Zod schema `EventTypeSchema` in `packages/api-types`; thread `kind` through `EventInputSchema` + event read schemas; update `contract.test.ts`.

### Migration
`web43a_event_kind_and_types` — add `Event.kind`, create `web_event_types`, seed rows, backfill `kind`. (Head is `data01_drops_player_date_idx`; chain off it or current head at build time.)

---

## 2. Feature B — Board-game event type

The board game is a **linear tile track** (~100 tiles, zig-zag) with a per-team game piece. The team always has exactly **one active task** (the task on the tile they're standing on). Progress streams in from real submissions against that one task; completing it earns **coins** and unlocks the team's **dice roll** — which moves the piece **X tiles forward** (X = dice result), landing on a new tile whose task (fixed or difficulty-rolled) becomes active. That **start → complete → roll → land** cycle is one **turn**, and `turns_completed` is the counter that drives item cooldowns. Tiles skipped over by the roll are not played (the dice choose which tiles a team lands on), but roadblock items placed on a skipped tile still trigger on pass-through. Coins buy items from a **shop**; items sit in the team **inventory** and are used on demand; each item **type** has a cooldown measured in turns. **Every mechanic here is config-gated** — dice, auto/manual roll, shop, per-item enablement, mercy rule, coin rewards, and win rule are all set per event (§2.7).

### 2.1 Data model (new `web_*` tables)

**Board layout (the designer's output):**
- **`web_event_board_tiles`** — one row per tile (analogous to `EventBingoCell` but with coordinates + task-or-difficulty):

  | column | type | notes |
  |---|---|---|
  | `id` | PK | |
  | `event_id` | FK web_events | |
  | `idx` | Integer | 0..N-1 sequence order along the track (drives advancement) |
  | `x`, `y` | Float | fractional 0..1 position on the background image (responsive) |
  | `label` | String(255) nullable | |
  | `difficulty` | String(24) nullable | **the default/primary mode** — a tier (air/water/earth/fire). On landing, the engine rolls a random task from the **event's own task pool** of this difficulty, so two teams (or the same team on a re-land) get different tasks |
  | `task_id` | FK web_event_tasks nullable | **optional override** — pin one specific task to this tile instead of rolling |
  | `tile_kind` | String(16) | `start` / `normal` / `special` / `finish` |
  | `config` | Text (JSON) | special-tile effects (bonus coins, teleport, etc.) |

  **Roll pool = the event's task list, not the global library.** A board-game event carries a pool of `EventTask` rows (added via the normal Tasks section, each tagged with a difficulty), and difficulty-tiles draw from that pool. This needs a new **`EventTask.difficulty`** column (`String(24)` nullable — today difficulty only lives on `EventTaskLibraryItem`; it must ride onto the event task when added so the pool is filterable). Setting a board is therefore mostly **placing tiles + assigning difficulties** (or one "auto-set", below) — you rarely pin specific tasks. Activation validation (`activation_blockers`): every difficulty used by a tile must have ≥1 task of that difficulty in the event pool, else landing has nothing to roll.

  **Per-landing task instances (progress isolation).** Because the same pool task can be rolled by many teams and re-rolled across turns, each landing **materializes a per-team task instance** — clone the chosen pool task into a fresh `EventTask` row tagged `config.board_instance=true` + `assigned_team_id`/`turn` (mirroring bingo's existing `bingo_auto` auto-task + GC pattern at `event_admin.py:605-840`). The team's `current_task_id` points at the instance, so `EventProgress(task_id, team_id)` and the whole ledger/rollup work unchanged and re-landing on the same pool task starts clean. Pinned (`task_id`) tiles instantiate the same way on landing.

- **`web_event_board_config`** — one row per event (board-level settings): `event_id` (unique), `background_url`, `bg_width`, `bg_height`, `tile_count`, `finish_tile_idx`, `starting_coins`, and a typed **`settings` JSON** holding the full config surface (§2.7). The `settings` schema (all with sane defaults so a leader can accept everything):
  ```jsonc
  {
    "movement": {
      "mode": "dice",              // "dice" | "fixed_step"
      "dice_count": 1, "dice_sides": 6,   // X tiles = sum of dice
      "fixed_step": 1,             // used when mode = fixed_step
      "trigger": "manual",         // "auto" (roll fires on completion) | "manual"
      "manual_roller": "team"      // who may trigger a manual roll: "team" | "group_admin" | "either"
    },
    "tile_render": {               // board designer / player overlay style
      "mode": "rune",              // "rune" (elemental rune icon by difficulty) | "invisible" | "outline"
      "outline_width": 2, "outline_color": "#ffcc33", "show_labels": true
    },
    "coins": { "enabled": true, "per_difficulty": {"air":10,"water":20,"earth":30,"fire":50}, "starting": 5 },
    "shop":  { "enabled": true, "rotation": "static", "rotation_turns": 0 },  // rotation_turns>0 = rotate every N turns
    "items": { "enabled_item_ids": [ ... ], "disabled_effects": [ ... ] },   // per-item + per-effect kill switches
    "mercy": { "enabled": true, "base_hours": 24, "step_hours": 12 },
    "win":   { "rule": "finish_tile", "tiebreak": ["coins","score"] }
  }
  ```

**Per-team runtime (the turn pointer — the missing core entity):**
- **`web_event_board_positions`** — `team_id` (PK, FK web_event_teams), `event_id`, `tile_idx` (current piece position), `current_task_id` (the active task; may be a rolled instance), `turns_completed` (the turn counter for cooldowns), `status` (`active`/`awaiting_roll`/`finished`/`blocked`), `blocked_until_turn` nullable, `updated_at`. Seeded at `activate_event()` (tile 0, turn 0, first task assigned).

**Coin economy (running-balance + audit ledger — simpler than FIFO; coins don't expire mid-event):**
- **`EventTeam.coins`** — new running-balance `Integer` column on the existing team row (mirrors `score`). Spend/earn are `+=/-=` under `SELECT … FOR UPDATE` on the team row.
- **`web_event_coin_ledger`** — append-only audit: `team_id`, `event_id`, `delta`, `reason` (`task_reward`/`purchase`/`refund`/`admin`/`bonus`), `ref_type`, `ref_id`, `balance_after`, `created_at`. (If you later need coin *expiry* or rotation-refunds, swap to the premium `PointCredit`/`PointDebit` FIFO shape — see D4.)

**Shop + inventory + power-ups:**
- **`web_boardgame_shop_items`** — site-wide curated catalog (superadmin-managed; the "osrs-esque items"):

  | column | notes |
  |---|---|
  | `id`, `key`, `name`, `description` | |
  | `icon_item_id` | OSRS item id for the icon (`/img/itemdb/{id}.png`) |
  | `item_type` | `movement` / `offensive` / `defensive` / `economy` / `utility` — **the cooldown grouping** |
  | `effect` | effect handler key: `skip_task`, `reroll_task`, `boost_coins`, `advance`, `roadblock`, `freeze_opponent`, `shield`, `steal_coins`, … |
  | `effect_config` | JSON params (magnitude, duration_turns, target rules) |
  | `cost_coins` | |
  | `type_cooldown_turns` | turns before the team may use **another item of this `item_type`** |
  | `active` | |

- **`web_event_shop_rotation`** — per-event rotation (the "rotating shop"): `event_id`, `shop_item_id`, `available_from_turn`/`available_until_turn` *or* wall-clock window, `price_override`, `stock` nullable. Rotation cadence in `board_config.settings`. (MVP: a static per-event selection; rotation is a Phase-3 scheduler.)
- **`web_event_team_inventory`** — `id`, `team_id`, `shop_item_id`, `acquired_turn`, `used_turn` nullable, `status` (`owned`/`used`/`expired`), `used_on` (JSON: target team/tile). Mirrors `FeatureActivation`.
- **`web_event_team_cooldowns`** — `team_id`, `item_type`, `last_used_turn`. Rule: usable iff `current_turn - last_used_turn >= type_cooldown_turns`. (Derivable from inventory, but a table keeps the check O(1) and matches legacy `event_team_cooldowns`.)
- **`web_event_effects`** — active roadblocks/boosts/shields: `id`, `event_id`, `source_team_id`, `target_team_id` nullable, `target_tile_idx` nullable, `effect_type`, `effect_config` (JSON), `expires_turn` nullable, `status` (`active`/`consumed`). Legacy `event_team_effects`. Roadblocks bind to a tile; boosts/shields bind to a team.

### 2.2 Engine changes (`services/event_engine.py`, `event_consumer.py`, `event_lifecycle.py`)

The whole engine assumes static all-tasks-live evaluation. Board-game mode needs a **branch keyed on `event.kind == "board_game"`**:

1. **Matcher state** (`load_matcher_state`, `:607`): for board-game events, load `web_event_board_positions` into a new `MatcherState` field `active_task_by_team: {(event_id, team_id): task_id}`. Keeps `handle_envelope` query-free.
2. **Task filtering** (`handle_envelope`, `:1416`): the single most important change. For board-game events, evaluate **only** the team's `current_task_id` instead of iterating `tasks_by_event`. (Standard/bingo path unchanged.)
3. **On task completion** (`apply_ledger_row`, in the completion block ~`:1261-1287`): after `progress.completed = True`, branch for board-game:
   - If `coins.enabled`: award **coins** = `coins.per_difficulty[tile.difficulty]` (applying any active `boost_coins` effect); write `web_event_coin_ledger` + bump `EventTeam.coins` under a row lock.
   - Set position `status = 'awaiting_roll'`.
   - If `movement.trigger == "auto"`: immediately perform the **dice roll** (step 4). If `"manual"`: leave `awaiting_roll` and wait for the roll action (step 4) from whoever `movement.manual_roller` permits.
   - Emit SSE `kind:"board_task_complete"` + a notification (new type `event_board_turn`, added to `EVENT_MESSAGE_TOGGLE_KEYS` at `events.py:114`, `KIND_FOR_TYPE`/`_COLORS`/`event_embed_spec` in `event_notifications.py`).
4. **The dice roll / advance** — a discrete step in `services/boardgame_engine.py`, callable both from the auto path (step 3) and from a manual web/Activity action (`POST /events/{id}/board/roll`, permission-checked against `movement.manual_roller`):
   - Roll `movement.dice_count`×`dice_sides` (or `fixed_step`) → **X**; clamp `tile_idx = min(tile_idx + X, finish_tile_idx)`.
   - **Pass-through check:** for each tile stepped over, trigger any active `roadblock`/effect bound to it (e.g. stop short, lose a turn) per `web_event_effects`.
   - Resolve the landed tile's task: if the tile pins a `task_id`, use it; otherwise **roll** a random task from the **event's task pool filtered to the tile's `difficulty`** (uniform random; if the pool for that difficulty is empty, fall back to any pool task + log). Materialize a per-team **instance** of the chosen task (§2.1) and point `current_task_id` at it. Set `status='active'`, `turns_completed += 1`; apply landed-tile special config; refresh matcher state.
   - **Win check:** `tile_idx >= finish_tile_idx` → mark position `finished`, fire win notification; first team to finish wins (tiebreak coins → score), then end the event (`end_event`).
   - Dice math is a pure, unit-testable helper (seedable RNG passed in — note `Math.random`/`Date.now` caveats don't apply server-side, but keep it injectable for tests); emit SSE `kind:"board_roll"` carrying the dice faces + destination for an animated client render.
5. **Activation seeding** (`activate_event`, `event_lifecycle.py:410`): initialize each team's `web_event_board_positions` (tile 0, turn 0, first task). Board-game readiness check in `activation_blockers()` (`:79`): require a saved board (≥ finish tile), ≥1 team, each team a piece.
6. **Coins scoring is additive** — no change to the `team.score += points` machinery; coins are a parallel balance.
7. **Item use is NOT submission-driven** — it's a web/Activity action (§2.4), applied synchronously by a new `services/boardgame_effects.py` (mirrors `GielinorRace._apply_item_effect`): validate ownership + type-cooldown + that the item/effect is enabled in `settings.items`, consume inventory, apply effect (skip/reroll/advance/roadblock/freeze/boost), write `web_event_team_cooldowns` + `web_event_effects`, bump matcher state, emit SSE/notification.
8. **Mercy / anti-stall** (config-gated by `settings.mercy`): a per-position task deadline (default ~24 h, +12 h per prior mercy). The lifecycle sweep (`run_lifecycle_sweep`, `:580`, already ticking ~60 s) auto-completes an overdue active task (reduced/zero coins) and fires the roll. Prevents unobtainable RNG from freezing a team; disable it for tightly-curated events.
9. **Revoke** (`revoke_ledger_row`, `:1538`): if an admin revokes the completion that triggered a roll, rewind the position (tricky — the dice result was random). MVP: block revoke on board-game completions that already advanced; surface an admin "rewind turn" action instead.

### 2.3 Board designer (frontend)

New `components/event-board-designer.tsx`, cloning the bingo designer's autosave block verbatim:
- **Background:** upload via the B2 server-side pattern (large user image) → `POST /events/{id}/board/background` → stored under `dt_` prefix, CDN URL saved to `web_event_board_config.background_url`. Or use the provided **sample board** (see §4).
- **Tile placement:** render the background; **click to drop a tile** at the click's fractional (x, y); tiles auto-number along a path. Drag to reposition; delete; reorder `idx`. A "zig-zag auto-layout" helper generates ~100 evenly-spaced tiles for a fresh board.
- **Per-tile editor** (modal like `CellEditor`): the **default and primary control is difficulty** (air/water/earth/fire — finally surfaced). A difficulty-tile rolls a random task from the event's per-difficulty pool on each landing (§2.1), so setting a board is mostly "place tiles + pick difficulties" — no per-tile task authoring required. As an **optional override**, a tile may **pin one specific task** (reuse the inline library search + `ItemNpcPicker` + custom-task tabs from the bingo `CellEditor`). Difficulty edits on a pinned/library task write back to the library row per your brief ("change it if it already exists").
- **"Auto-set tile type" button** — one click cycles **air → water → earth → fire** across all placed tiles in order (tile 1 = air, 2 = water, 3 = earth, 4 = fire, 5 = air, … i.e. `["air","water","earth","fire"][idx % 4]`; matches the legacy `_generate_tiles` cadence). **Guarded:** if any tile already carries a *different* difficulty (or a pinned task), show a confirm dialog first ("this overwrites difficulties on N tiles"); if every tile already matches the cycle it's a silent no-op. After running, individual tiles remain fully editable — the cycle is just a fast starting point. Applies through the same `updateCell`→autosave path.
- **Configurable tile rendering (`settings.tile_render.mode`)** — a board-level toggle in the designer that controls how tiles paint over the background, both in the designer and in the live player view:
  - **`rune`** — each tile shows the **elemental rune icon** for its task's difficulty (Air rune `/img/itemdb/556.png`, Water `555`, Earth `557`, Fire `554`; neutral marker if no difficulty). Placed-and-typed tiles become self-explanatory.
  - **`invisible`** — no overlay; the background art *is* the board (for leaders whose uploaded image already draws tiles). Tiles remain clickable hotspots in the designer (shown only on hover) but paint nothing for players.
  - **`outline`** — a bordered hotspot with configurable `outline_width` + `outline_color`; lets a leader trace their own artwork's tiles without hiding it.
  The designer has a live preview that switches with the mode so the leader sees exactly what players will see.
- **Autosave:** `buildInput()` emits `{ background, tile_render, tiles: [{idx, x, y, label?, task_id|library_item_id|difficulty|new_task, tile_kind, config}] }` → server action `saveEventBoard` → `PUT /events/{id}/board` (new route in `event_admin.py` modeled on `put_bingo_board:693` — replace-whole-board, GC orphan auto-tasks, flip `Event.kind='board_game'`, `_assert_board_editable` gate, `AuditLog`).
- Mount a new "Board" section in `event-manager.tsx` gated on `event.kind === 'board_game'` (bingo designer gated on `'bingo'`).

### 2.4 Coin economy + shop + power-ups (frontend + API)

- **Shop catalog admin** (superadmin): new `admin/boardgame-shop/page.tsx` cloning `points-manager.tsx`'s section-per-concern layout — a `CatalogSection` (CRUD `web_boardgame_shop_items`, icon via `ItemNpcPicker`), effect-config editors per effect type. API in a new `web_api/routes/boardgame.py` (or extend `event_admin.py`): GET/POST/PATCH/DELETE + `_audit`.
- **Per-event shop rotation** editor in `event-manager.tsx` (which catalog items are available, price overrides, rotation window) — mirror `points-manager.tsx` `BoostsSection` (timed windows).
- **Team-facing game view** (`event-view` / a new `components/event-boardgame-view.tsx`, plus Activity parity later): renders the board + pieces (SSE-live), the team's **coin balance**, **current task** + progress, **inventory**, the **shop** (buy → `POST /events/{id}/board/shop/buy`), and **use-item** buttons (→ `POST /events/{id}/board/items/{invId}/use` with optional target). Only team members (or admins) can spend/use; enforce in the route (reuse `assert_group_member`/team-membership checks).
- **Buy/use flow** mirrors `activate_feature_for_group()` — one `session.begin()`: lock team row, check balance/cooldown, debit coins + ledger row, insert/flip inventory, apply effect. Reuse the `-=` refund pattern for undo.
- **Power-up catalog (starter set, from `GielinorRace`):**

  | item | type | effect | notes |
  |---|---|---|---|
  | Teleport tablet | movement | `advance` N tiles | forward jump |
  | Task reroll (scroll) | utility | `reroll_task` | redraw current tile's task (for unobtainable RNG) |
  | Skip token | utility | `skip_task` | complete current task w/o submission (reduced coins) |
  | Coin boost (chest) | economy | `boost_coins` 2× next task | timed effect on self |
  | Dinh's Bulwark | defensive | `roadblock` on a tile | stalls the next team to reach it 1 turn |
  | Ice barrage | offensive | `freeze_opponent` N turns | target can't advance |
  | Shield | defensive | `shield` | negates next offensive effect |

  Each carries `type_cooldown_turns`; a team may only use one item **of that type** within that many turns (turn = completion count from `web_event_board_positions.turns_completed`).

### 2.5 Event configuration surface (maximum configurability — first-class requirement)

Everything above is driven by `web_event_board_config.settings` (§2.1). Rather than scatter these toggles, expose them through **one typed config surface** so a leader has full control and nothing is hard-coded:

- **Backend contract:** a per-event board-config registry mirroring the group-config pattern (`web_api/config_registry.py`) — field defs, types, defaults, bounds, coercion — so validation is centralized and the UI is generated from it. `GET/PATCH /events/{id}/board/config`, `_assert_event_admin`-gated + `AuditLog`. Every read of a mechanic (engine, effects, roll) goes through a single `board_settings(event)` accessor that applies defaults, so a partial config never crashes a mechanic.
- **Frontend:** a "Board settings" section in `event-manager.tsx` (gated on `kind==='board_game'`), grouped like `points-manager.tsx`: **Movement** (dice count/sides or fixed step; auto vs. manual; who may roll), **Board appearance** (tile render mode + outline width/color + labels), **Economy** (coins on/off, per-difficulty rewards, starting coins), **Shop** (shop on/off, rotation cadence, **per-item enable checklist**, per-effect kill switches), **Mercy** (on/off + hours), **Win** (rule + tiebreak). Each subsection autosaves (reuse the debounce pattern).
- **Kill switches all the way down:** `shop.enabled=false` hides the whole economy UI + shop endpoints; `items.enabled_item_ids` / `items.disabled_effects` remove individual items/effects from the shop and reject their use server-side; `coins.enabled=false` turns the board into a pure race with no economy. A leader can dial the game from "just a task race on a pretty board" up to "full PvP with sabotage."

### 2.6 Team game pieces (icons)
- Add **`EventTeam.piece_item_id`** (OSRS item id — zero-upload, render via `ItemDbIcon`) and/or **`EventTeam.piece_icon_url`** (custom upload via the group-icon local-tree pattern). MVP: `piece_item_id` picked with `ItemNpcPicker mode="single" kind="item"`.
- Extend `EventTeamSchema` + `EventTeamPatchSchema` + `updateEventTeam` (`PATCH /events/{id}/teams/{teamId}`) to accept the piece; render alongside the existing color dot in `event-teams-panel.tsx` / `event-team-view.tsx` and on the board.

### 2.7 Migrations
- `web44a_board_game_core` — `Event.kind` already added in web43a; add `EventTeam.coins` + `piece_item_id`/`piece_icon_url`; **add `EventTask.difficulty`** (`String(24)` nullable — carries difficulty onto event tasks so difficulty-tiles have a filterable roll pool); create `web_event_board_tiles`, `web_event_board_config`, `web_event_board_positions`, `web_event_coin_ledger`.
- `web45a_board_game_economy` — `web_boardgame_shop_items`, `web_event_shop_rotation`, `web_event_team_inventory`, `web_event_team_cooldowns`, `web_event_effects`.
- Optionally register new models in `web_api/admin_registry.py` (`ENTITY_REGISTRY`) for free superadmin data-viewer access.

---

## 3. Suggested phasing (MVP-first)

| Phase | Scope | Ships |
|---|---|---|
| **P0 — Event types** | `Event.kind` + `web_event_types` registry + **test-group allowlist** + toggle panel + creation gate + create-form selector | The type framework; board_game exists but empty. **Small, do first.** |
| **P1 — Board core** | Board tiles/config models + **config surface (§2.5)**, board designer (reuse autosave) w/ configurable tile rendering, turn engine (active-task filter + **dice roll/advance** + auto/manual trigger + coins), team piece, live team board view w/ SSE | A playable dice board *without* a shop. Admin-only. |
| **P2 — Economy & beneficial items** | Coin ledger polish, shop catalog admin, per-event shop, buy/inventory, self/utility power-ups (skip/reroll/boost) + type-cooldowns, mercy rule | Teams earn & spend coins on their own team. |
| **P3 — Interference & polish** | Offensive/defensive/roadblock effects, rotating-shop scheduler, Discord/Activity parity (board render + turn notifications), templates support, revoke/rewind admin tooling | Full PvP board game. Flip `board_game` on for wider use. |

Each phase is independently shippable and testable; P0 is a prerequisite; P1 is the big lift (engine branch + designer).

---

## 4. Sample board asset (your requested deliverable)
Generate a ~100-tile zig-zag board background (OSRS-esque artwork) plus a matching default tile-coordinate JSON so the designer can seed a board in one click. Approach: procedurally render an SVG→PNG (serpentine path, numbered tiles, themed tile art per element tier) written to `static/assets/img/events/board-default.png`, with coordinates emitted as a `board_config` preset. This is a build task in P1, not part of the framework.

---

## 5. Decisions

### Resolved (this review)
- **Scope** → full **P0→P3** arc, phased.
- **Movement** → **dice** (roll X tiles per turn), but fully **configurable** per event: dice count/sides or fixed step; **auto vs. manual** trigger; who may roll (team member / group admin / either). Win → **first to the finish tile** (tiebreak coins → score).
- **Configurability** → first-class from day one (§2.5): shop on/off, per-item + per-effect kill switches, coins on/off + per-difficulty, mercy on/off, tile rendering.
- **Disabled-type gate** → **superadmin + per-type test-group allowlist** (`web_event_type_test_groups`).
- **Tile rendering** → configurable **rune / invisible / outline** (§2.3).
- **Tile task source** → **difficulty-roll is the default** (rolls from the event's per-difficulty task pool per landing); pinning a specific task is an optional override; a designer **"auto-set tile type"** helper cycles air→water→earth→fire (confirm-guarded). Needs new `EventTask.difficulty` + per-landing task instances (§2.1).

### Still open (safe defaults chosen; flag if you disagree)
- **Coin store** — **running balance on `EventTeam.coins` + append-only audit ledger** (simple, per-event closed economy). Switch to the premium FIFO `PointCredit`/`PointDebit` shape only if coins must expire or rotation must refund.
- **Shop model** — **site-wide curated catalog + per-event rotation/selection** (consistent items, per-event flavor + per-item enable toggles). Alternative: fully bespoke per-event shop.
- **Team piece** — **OSRS item icon** (`piece_item_id`, zero upload) for MVP; custom-image upload added in P3.

---

## 6. Rough effort (relative)
- P0 Event types: **S** (1 migration, 1 service, 2 endpoints, 1 admin page, create-form tweak).
- P1 Board core: **L** (engine branch is the hard part; designer clones existing autosave; 4 tables).
- P2 Economy: **M–L** (ledger + shop admin UI clone + buy/use engine + cooldowns).
- P3 Interference/polish: **L** (effect system, rotation scheduler, Discord/Activity parity).
