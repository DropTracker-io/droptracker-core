# SOTW / BOTW — the `sotw` / `botw` competition event kinds (web105a)

Skill of the Week / Boss of the Week: a time-boxed race of INDIVIDUALS —
most XP gained in one skill (`sotw`) or most KC gained at one boss / NPC
group (`botw`) between the event's start and end. Any duration ("of the
Week" is branding). Feature parity with WiseOldMan's competition bot
(lifecycle announcements, standings) plus DropTracker-only enrichments:
live plugin tracking between hiscores updates, a self-updating Discord
leaderboard message, and **bonus points** (pets, sub-threshold kill times)
awarded from plugin submissions with proof attached.

This file is the maintained spec. The two kinds share one implementation;
the kind only pins the metric family (`skill` vs `boss`) and presentation.

## Architecture (what an event is made of)

Following the loot_sweep recipe — almost no new storage:

- **One hidden task, `type: "competition"`** carrying the whole scoring
  config in its `config` JSON (below). Every crediting envelope lands on
  this ONE task, so per-player standings fold from a single ledger. It
  never "completes" (`EventProgress.completed` stays False; `record_match`
  would stop recording otherwise). Managed only through the event's
  competition settings — the generic task routes 422 on it
  (`validate_task_payload`, the PATCH/DELETE guards), and serializers flag
  it `"managed": true`.
- **One roster team** ("Participants"). Participation mode lives nowhere
  new: `team.auto_clan` IS the fact — `whole_clan` sets
  `auto_clan=True, group_id` (the matcher expands the clan's current
  membership; `sync_auto_clan_rosters` mirrors it, its `clan_vs_clan` gate
  widened to admit competition kinds), `signup` leaves a plain team with
  `formation_mode="auto_assign"` so the existing sign-up flow places
  players with no team-pick step. Activation requires ≥1 team — the
  scaffold satisfies it.
- **One `web_event_competitions` row** (1:1, hosted rows too): WOM linkage
  + sync state + the standings cache for WOM participants with no DT
  account + the frozen `final_standings`. `wom_competition_code` is a
  SECRET (created mode's edit/delete credential) — never serialized,
  excluded from every payload.
- Scaffold lifecycle: `services/competition_setup.py::ensure_competition_scaffold`
  (create/PATCH/activation-heal), `remove_competition_scaffold` (draft kind
  switch away). Config and source mode lock at activation (409).

## Task config (`services/competition.py` is the parser + pure scoring)

```jsonc
{ "kind": "competition",
  "metric_kind": "skill" | "boss",
  "skill": "mining",                    // sotw; wom_skill_metric-validated ("overall" deferred)
  "npcs": ["Dagannoth Rex", "..."],     // botw; ≤10, canonical NpcList names, aliases expanded
  "ranking": {
    "mode": "gained" | "points",        // gained = WOM-parity board, bonuses a side column
    "gained_per_point": 10000           // points mode: floor(gained/rate) + bonuses, one ranking
  },
  "bonus_rules": [                      // ≤6; one pet rule + any number of time tiers
    { "id": 1, "type": "pet", "points": 50, "max_awards": 1,
      "pets": ["Rock golem"] },         // sotw auto-fills the skill's skilling pet
    { "id": 2, "type": "time_under", "npc": "Dagannoth Rex",
      "threshold_ms": 25200, "points": 5, "max_awards": 3 }   // per-player cap
  ] }
```

Validation: `web_api/routes/event_task_validation.py::validated_competition_config`
(bounds mirrored from `services/competition.py`; `tests/unit/
test_competition_validation.py::TestBoundsMirror` keeps the copies honest).

## Scoring (pure-from-ledger, like loot_sweep)

- Matcher (`event_engine.match_task` competition branch): `experience`
  envelopes → the existing xp baseline fold; `drop`/`wom_kc` → the existing
  per-NPC KC watermarks (plugin+WOM merge by MAX, never sum — no double
  counting); `pet` (new pets only) → a bonus row; `pb` (EVERY kill time is
  queued) → one bonus row PER qualifying time tier (`match_task_all` — a
  0:48 kill under both "sub-1:00" and "sub-0:50" awards both).
- Bonus rows are ordinary `EventCompletion`s tagged
  `note = "bonus:{type}:{rule_id}"` (+ ` | 0:52.6` kill time in the human
  half) with guid suffix `#b{rule_id}` — auditable, revocable, proof
  attached. `quantity` = the points.
- `fold_rows` splits gained vs bonus BY NOTE and enforces per-player caps
  in the fold itself (defense in depth: revoking an award frees its slot;
  an over-cap row that slipped a gate pays nothing). The record-time gate
  is `_row_advances_progress`.
- `_apply_competition` / `_revoke_competition`: re-fold →
  `EventProgress.progress` = team GAINED total, `EventTeam.score` = the
  ranking-mode total, SSE frame (`kind: "competition"`, rank/leader
  included), bonus rows enqueue `event_competition_bonus`.
  **`_award_contribution_points` is deliberately not called per row** (an
  XP-snapshot stream would rewrite the roster thousands of times a day) —
  `finalize_competition` writes `EventPlayerPoints` once at end.
- Standings are a ledger aggregation (`GET /events/{id}/competition`),
  never a stored rollup; past events serve the frozen `final_standings`.

## Source modes (`web_event_competitions.source_mode`)

- **hosted** — plugin envelopes + the group WOM reconciler
  (`event_wom_reconciler`, whose `RELEVANT_TASK_TYPES`/`_relevant_metrics`
  know `competition` tasks). Non-WOM-ranked bosses run plugin-only
  (activation warns, never blocks).
- **linked** — mirrors an EXISTING WOM competition:
  `services/competition_wom.py` polls `GET /competitions/:id`
  (`WOM_COMPETITION_POLL_SECONDS`, default 300s; ~1 req/event/cycle — 20
  concurrent comps ≈ 4% of the shared limiter) and emits the per-
  participant window deltas through the reconciler's own `_emit_for_row`
  (same envelopes, seen-gates, clamps, `xp_start`/`kc_start` seeds).
  Unmatched participants land in the `wom_standings` cache (greyed
  display rows; no bonuses — bonuses need plugin data). **WOM owns the
  window**: link copies the comp's dates onto the event, each poll
  re-syncs drift, and the DT-side date PATCH 422s. The raw JSON fetch
  (`utils/wiseoldman.get_competition_raw`) is deliberate: the pinned
  wom.py fork's typed model predates multi-metric comps, and the link
  validator must SEE the `metrics` array to refuse multi-metric and
  team-type comps (v1 races one classic metric).
- **created** — DropTracker creates the WOM competition
  (`POST /events/{id}/competition/wom-create`, draft-only, idempotent;
  needs `Group.wom_id` + the `wom_verification_code` group config; single
  WOM-ranked boss / one skill). The returned competition code is stored
  and buys write mirroring: DT-side name/date edits push out
  (`edit_wom_competition`), delete/kind-switch/unlink delete the comp,
  a DT-side end closes it (`mirror_competition_end` — web route + sweep),
  and the poller fires competition-scoped `update-all` inside the lead
  window before start/end (`_womcompupdall` flags). Then it behaves like
  linked.
- End-of-event: the consumer drains `pending_final_competition_ids` →
  `final_competition_poll` (cache-busting, force) BEFORE the lifecycle
  sweep ends the event, so the frozen result includes WOM's last word.

## Discord

- New notification types: `event_starting_soon` / `event_ending_soon`
  (KIND-AGNOSTIC one-shot reminders, fired by the lifecycle sweep at
  `starts_at`/`ends_at` minus `message_config.reminders.*_lead_minutes`,
  default 60, Redis NX per (event, side)); `event_competition_bonus`
  (default ON — the kind's signature moments); `event_competition_milestone`
  (reserved, default OFF). Layout defaults + TYPE_META/TOKEN_DOCS in
  `services/event_message_layouts.py`; new tokens
  `{competition_metric_line}` (started/ended/reminders),
  `{competition_summary_line}` (live board), bonus award tokens.
- The live leaderboard message (`services/event_board.py`) renders PLAYER
  standings with pre-worded `score_text` ("2.48M XP" / "312 KC" /
  "270 pts" — `standings_lines` prefers `score_text` everywhere), the
  "⚔️ N players · X gained" headline, and refreshes on bonus awards.
- The sender resolves player standings for competition events on
  `event_ended`/`event_lead_change`/`event_ending_soon`, and the pet-award
  thumbnail via the usual `received_item` icon path.
- Board image: `_competition_signature` (ordered per-player gained/bonus)
  drives the screenshot cache; the web page `/board-image/{id}` renders
  `CompetitionStandingsSnapshot` (top 10).

## Web

Frontend (web repo): `EVENT_KINDS` + full Zod contract in
`packages/api-types`; `lib/competition.ts` (pure helpers, unit-tested,
mirrors the backend wording); wizard flow for competition kinds is
**Basics → Schedule → Competition → Joining & rules → Discord → Review**
(`components/competition-setup.tsx` — metric picker, WOM link/validate/
create, ranking, bonus builder, participation); public page renders the
standings table (`competition-standings.tsx`, SSE-refetching, WOM rows
greyed, row-expand award log) + "How the race is scored" card; the manager
gets a Competition tab and hides Tasks/Teams/Board; `/events/{id}/teams`
404s. Mock mode ships a full botw fixture (event id 6).

## Routes (web_api, `/api/v1`)

`GET /events/{id}/competition` (+ render-token bypass) ·
`GET /events/{id}/competition/players/{playerId}` ·
`GET /events/meta/wom-competition?query=` (preview/validate; 5-min-cached
raw fetch) · `GET /events/meta/wom-readiness?group_id=` (boolean-only) ·
`POST/DELETE /events/{id}/competition/wom-link` ·
`POST /events/{id}/competition/wom-create` · `competition` block on
POST/PATCH `/events` (draft-only once created) · `/events/meta/npcs` rows
carry `wom_metric` for the picker badge.

## Deploy / ops runbook

1. **Web deploys first** (`scripts/deploy.sh`): the api-types superset must
   land before the registry rows exist or `/admin/event-types` Zod-500s.
2. **Owner applies the DDL** — `alembic/versions/web105a_event_competitions.py`
   (table + the two `web_event_types` INSERTs, shipped `enabled=0,
   admin_only=1`). NB the chain tip is `web100a_player_name_norm_index`
   (nonlinear numbering — web100a postdates web104a).
3. **Deploy disc**: restart `droptracker-webapi`, `droptracker-events`,
   `droptracker-core`. (Producers untouched — webhook-consumer keeps
   running.) Optional env: `WOM_COMPETITION_POLL_SECONDS`.
   NOTE: the starting/ending-soon reminders go live for ALL kinds at this
   moment (defaults ON, 60-min lead).
4. Seed layouts: `venv/bin/python -m scripts.seed_event_message_layouts
   --types event_starting_soon,event_ending_soon,event_competition_bonus,event_competition_milestone`
   then (diff group-1 rows first — `--force` clobbers staff edits)
   `--force --types event_started,event_ended,event_board` for the token
   additions. Restart `droptracker-core`.
5. Flip `enabled=1` (keep `admin_only=1`), add beta clans via
   /admin/event-types, run the beta checklist below, then drop admin_only.

**Beta checklist:** hosted sotw (points mode, pet bonus) — blockers UX; no
retroactive credit on the first snapshot; WOM top-up credits a plugin-less
member; bonus message + revoke self-heals the cap; reminders at T-60;
final standings frozen + EventPlayerPoints written once. Linked botw
against a real comp — preview/link, date drift sync, greyed WOM rows, live
board edits in place, limiter logs clean. Created botw — comp appears on
WOM, PATCH title mirrors, draft-discard deletes it.

## Deferred (deliberate v1 cuts)

Teams / WOM team comps / clan_vs_clan mapping · "overall" as a sotw metric
(mixed-baseline double-count trap needs its own design) · multi-metric
comps · recurring-window competitions (blocked at activation) ·
`/event standings` slash command · overtake/milestone messages
(`event_lead_change` emission from `_apply_competition` has rank/leader
ready; `event_competition_milestone` reserved) · duplicate-pet bonus ·
create-on-WOM for groups without `wom_id` (explicit-participants mode) ·
event templates for competition kinds (save/instantiate 422 with copy).

Tests: `tests/unit/test_competition.py` (pure), `test_competition_engine.py`
(matcher/gate/apply/revoke), `test_competition_wom.py` (parse/verdicts/
emission vs a recorded fixture), `test_competition_validation.py`
(validator + bounds mirror + managed-task guard); web
`apps/web/test/competition.test.ts` + contract tests.
