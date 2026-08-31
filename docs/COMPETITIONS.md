# SOTW / BOTW — the `sotw` / `botw` competition event kinds (web105a)

Skill of the Week / Boss of the Week: a time-boxed race of INDIVIDUALS —
most XP gained in one skill (`sotw`) or most KC gained at one boss / NPC
group (`botw`) between the event's start and end. Any duration ("of the
Week" is branding). Feature parity with WiseOldMan's competition bot
(lifecycle announcements, standings) plus DropTracker-only enrichments:
live plugin tracking between hiscores updates, a self-updating Discord
leaderboard message, and **bonus points** awarded from plugin submissions with
proof attached — pets, sub-threshold kill times, gained milestones, and
anything else the event task builder can express, scoped to the raced target.

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
  "bonus_rules": [                      // ≤12, mixed freely
    { "id": 1, "type": "pet", "points": 50, "max_awards": 1,
      "pets": ["Rock golem"] },         // auto-filled: sotw = the skill's
                                        // skilling pet, botw = whatever the
                                        // raced boss drops (wiki drop tables)
    { "id": 2, "type": "time_under", "npc": "Dagannoth Rex",
      "threshold_ms": 25200, "points": 5, "max_awards": 3 },  // per-player cap

    // ANY criteria the task builder can express, embedded verbatim and
    // evaluated by the engine's own match_task against a synthetic task dict.
    { "id": 3, "type": "task", "points": 40, "max_awards": 1,
      "label": "The full Zulrah set",
      "task": { "type": "item_collection", "target": null, "target_value": 3,
                "config": { "kind": "all_of", "items": [...],
                            // INJECTED by the validator — never trusted from
                            // the client. This is the scoping guarantee.
                            "item_npcs": { "Tanzanite fang": ["Zulrah"], ... },
                            "clog_sources": true } },
      // Derived ONCE at write time so services/competition.py needs no task
      // vocabulary, and a config whose shape drifts can't rewrite history.
      "progress_kind": "distinct",      // count|distinct|groups|any_path|points
      "need": 3,                        // progress units per award
      "kinds": ["drop", "clog", "pet"], // envelope pre-filter
      "scope": ["Zulrah"] },            // display only

    // Gained milestones. Zero ledger rows — folded straight off `gained`, so
    // they cannot double-count and need no matcher.
    { "id": 4, "type": "milestone", "step": 100, "points": 10,
      "max_awards": 20 }
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
- **`task` rules** are the whole task-builder vocabulary for one new rule
  type. `_task_to_dict` turns each embedded `{type, target, target_value,
  config}` into a SYNTHETIC task dict (`_enrich_matcher_precompute`, shared
  with real tasks), and `match_task_all` runs the same `match_task` over it.
  Matches whose `mode` isn't `count`/`first` are DROPPED — a kc/kc_abs/xp
  match would reach the shared KC-watermark and XP-baseline folds, which are
  scoped by task, and the task here IS the race. (The validator 422s
  `kc_target`/`xp_target` as embedded types; this is the second lock.)
- **Scoping is enforced, not promised.** `validated_competition_config`
  overwrites the embedded config's `item_npcs`/`source_npcs` with the raced
  NPCs — wholesale, because `_item_source_npcs` fails OPEN for an item with no
  entry, so a partial map is a partial scope. The runtime gate that already
  enforces it for real tasks then enforces it here. A sotw may only embed
  `pet_collection`/`skill_target`: there is no item→skill dataset anywhere in
  the codebase, so a drop bonus on a skill race could only mean "from
  anywhere".
- **`milestone` rules** ("every 100 kills = 10 pts") fold off the gained total
  and write no ledger rows at all — nothing to double-count, nothing to
  revoke. `_announce_milestone_crossings` diffs the prev/curr folds to fire
  the award message.
- Bonus rows are ordinary `EventCompletion`s tagged
  `note = "bonus:{type}:{rule_id}"` (+ ` | 0:52.6` kill time in the human
  half) with guid suffix `#b{rule_id}` — auditable, revocable, proof
  attached. **Two ledger dialects**, told apart by the note's type segment:
  for `pet`/`time_under` one row IS one award and `quantity` is the points;
  for `task` one row is one unit of PROGRESS and the points come from the rule
  at fold time (`points` per `need`, capped at `max_awards`). Never read a
  bonus row's quantity as points without checking its type —
  `_apply_competition` reports `curr_bonus_points - prev_bonus_points`, which
  is correct under both.
- `parse_bonus_note` matches the type segment on SHAPE, not against
  `BONUS_RULE_TYPES`: an unparsed row folds into GAINED, so failing closed on
  a type a newer deploy wrote would silently inflate the ranked metric.
  Recognised-but-unknown pays nothing, which is wrong but bounded.
- `_dedupe_clog_echo` covers competition `task` rules too, scoped to the
  candidate's own rule tag — every rule on a competition shares one task id,
  so without the tag filter rule 3's drop would suppress rule 5's clog.
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
create, ranking, bonus builder, participation). The bonus builder EMBEDS the
real task builder for `task` rules (`EventTaskForm`'s `onDraftSubmit` draft
mode, the same hook the bingo cell editor uses), restricted per race kind via
`omitTypes` — so a bonus gets every item picker, source restriction and
collection mode a real task has, with zero duplicated UI; public page renders the
standings table (`competition-standings.tsx`, SSE-refetching, WOM rows
greyed, row-expand award log) + "How the race is scored" card; the manager
gets a Competition tab and hides Tasks/Teams/Board; `/events/{id}/teams`
404s. Mock mode ships a full botw fixture (event id 6).

## Routes (web_api, `/api/v1`)

`GET /events/{id}/competition` (+ render-token bypass) ·
`GET /events/{id}/competition/players/{playerId}` ·
`GET /events/meta/wom-competition?query=` (preview/validate; 5-min-cached
raw fetch) · `GET /events/meta/wom-readiness?group_id=` (boolean-only) ·
`GET /events/meta/ca-monsters` (combat achievement bosses + per-tier counts —
the counts are the point: "Zulrah, Master" must read as two tasks before it is
saved) ·
`POST/DELETE /events/{id}/competition/wom-link` ·
`POST /events/{id}/competition/wom-create` · `competition` block on
POST/PATCH `/events` (draft-only once created) · `/events/meta/npcs` rows
carry `wom_metric` for the picker badge.

## Combat achievements (`ca_target`, a real task type)

A CA envelope (`data/submissions/ca.py`) carries `task_name` and `tier` and no
NPC, so "a Hard CA at Zulrah" is not expressible at match time. It IS
expressible at AUTHORING time: `scripts/ca_tasks.json` — 655 records extracted
from the game cache and already shipped in the `combat_achievement_tasks`
manifest section — carries `monster` and `tier` per task.

`utils.ca_tasks.tasks_for_monsters(session, monsters, tiers)` resolves that to
an explicit allow-list of task NAMES, stored in `config.task_names` exactly
like a customized pet list. Matching then stays pure. An unreadable registry
503s the write rather than storing an empty list: the matcher reads "no list"
as "credit nothing", and a task that silently counts nothing all event is
worse than a save that fails loudly.

`ca_target` is a first-class task type (`EVENT_TASK_TYPES`, `AUTO_TASK_TYPES`,
the task builder's own editor with a boss picker and tier chips backed by
`GET /events/meta/ca-monsters`), so it works in ordinary events as well as
inside a competition bonus rule.

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

## Bonus-task extension (2026-08-30) — what changed

- **No DDL and no alembic revision.** Everything rides in the existing hidden
  task's `config` JSON. No layout re-seed either: the award message's
  "where it counted" wording rides inside the existing `{bonus_reason_line}`
  token rather than a new one, precisely so every group's layout stays valid.
- **Deploy order still matters**: web api-types ship first (the `type` field
  on `CompetitionBonusRuleSchema` is now a bare `z.string()` so an unknown
  rule type degrades instead of 500-ing every viewer's event page), then the
  backend in ONE restart — a config the validator accepts but
  `services/competition.py` rejects gets its rule silently dropped and its
  `max_awards` downgraded to 1.
- **Two live defects fixed on the way**: `+ Pet bonus` on a boss race hard-422'd
  ("Missing pets") because only skill races had a default and the wizard never
  sent an explicit list — botw now auto-fills from the wiki drop tables
  (`db.item_sources.items_dropped_by_npcs`, boss/raid pet categories only). And
  a skill with no skilling pet now says so instead of 422-ing "Missing pets".
- Several pet rules may now coexist as long as their lists are DISJOINT (a pet
  paying twice was the reason for the old one-rule limit).
- The pure progress folds moved to `utils/task_progress.py` (behaviour
  unchanged; `event_engine` keeps its private names as aliases). They had to
  leave `services/` because the pytest conftest MagicMocks that whole package,
  so `services.competition` could not have imported them.
- `competitionBlockToInput` used to hand-copy six named fields, which would
  have ERASED every task rule on a manager load→save round trip. It now drops
  the read-only projection keys instead of naming the ones to keep.

**Traps an adversarial review found in this change** (all fixed; each has a
regression test, and each would have been invisible in normal use):

- **Weighted pools were weighted twice.** `item_match_quantity` already
  returns an item's POINT VALUE as the credit for a `point_collection` config,
  so folding those rows through `points_fold` squared them — one 300-point
  drop against a 500-point goal folded to 90,000 and maxed the rule. The
  `points` progress kind now sums pre-weighted rows flat. (An `any_path`
  POINTS *path* is the opposite case — its rows are NOT pre-weighted — which
  is why `anypath_progress_from_rows` still folds through `points_fold`.)
- **`skill_target` paid on every XP snapshot.** Its match condition is a
  persistent STATE ("level ≥ 70"), not an event, so `max_awards > 1` paid
  again on the next experience envelope. `SINGLE_AWARD_TASK_TYPES` pins it to
  one award, in the SCORER as well as the validator, so configs already stored
  are corrected on read.
- **Either-or METRIC branches were dead.** Metric paths are matched only in
  `match_task_all`'s post-loop block, which the competition branch returns
  before — a "full set OR 5,000 KC" bonus would have shown a reachable second
  branch that could never credit. Refused at write time.
- **A skill race's pet bonus stored as "any pet".** The task-shaped pet goal
  had no drop table to fall back on, so it collapsed to the bare "any non-misc
  pet, from anywhere" form; and an explicit `categories` selection was dropped
  entirely, which WIDENED it. Both fixed.
- **`note LIKE "bonus:task:1%"` also matched rules 10–12.** The clog-echo and
  vestige-chain lookups now compare the tag exactly (a task row has no ` | `
  human half — only `time_under` writes one).
- **The DT2 ring/vestige pity chain double-credited.** `_ring_vestige_for_task`
  rewrites each Gold ring to the vestige name for the synthetic embedded task
  too, so three drops counted as three items. `_dedupe_vestige_chain` now
  covers the `task` dialect, rule-scoped like the clog echo.
- **A milestone named the wrong unit** — the label guessed "XP" vs "kills"
  from the step's magnitude, so a 1,000-kill boss milestone read "Every 1,000
  XP". It now reads the race's metric kind.
- **An admin's rule name was lost on re-save.** The serialized `label` is the
  sentence to SHOW (derived when unnamed); echoing it back would have frozen
  derived text into the config. `custom_label` carries the admin's own wording
  and is the half that round-trips.
- **A blanked `target` serialized as `null` and failed the input schema** on
  the manager's own round trip — every embedded type but `pb_target`,
  `skill_target` and a single-item collection blanks it.

## Deferred (deliberate v1 cuts)

Live (post-activation) bonus edits — the config still locks at activation, so
an admin who wants another bonus mid-race must wait; append-only forward-only
edits are the intended follow-up (editing an existing rule's `points` is NOT
safe: a task rule's points come from the rule at fold time, so it would rewrite
history) · sotw drop bonuses (no item→skill dataset exists) ·
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
