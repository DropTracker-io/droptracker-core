# Event task generator ("Fill for me")

Status: **live 2026-09-24** (disc `823d495`, web `acaf8a2`). Library tidy applied; starter templates not seeded yet.

## Why

Events stall in draft at the task step. On 2026-09-24, 11 of the 19 drafts had
two tasks or fewer, several were abandoned within a minute of being created,
and two bingo boards had all 25 cells but 0 and 2 tasks. Finished bingos
average about 16 tasks and finished board games about 60, all picked one at a
time from a flat library:

- 258 active public presets, 209 of them `item_collection`. There were 13 KC,
  8 PB, 5 pet and 1 CA preset, and no slayer, EHB or EHP presets at all.
- No categories: the only filters are name, type and the legacy rune tiers.
- Quality varies: duplicates, placeholders, manual-only legacy rows.
- Only 5 public templates, used 13 times in total.

The fix is to stop making admins build lists by hand. DropTracker already has
the data needed to generate a good list: drop rates, kill rates, curated boss
uniques, and what each clan actually kills.

## What it does

An admin fills in a few settings and gets a balanced, sized preview:

| Input | Default |
|---|---|
| Task count | board size² for bingo, 40 for board games, otherwise 12 |
| Event length (days) | from `starts_at`/`ends_at`, otherwise 7 |
| Players per team | average roster size, otherwise 5 |
| How active | casual 0.75 h/day · regular 1.5 · very active 3 |
| Difficulty mix | Balanced 3/3/2/1, Mostly easy, Challenging, Elite only, Custom |
| Content | raids, GWD, DT2, other bosses, slayer, wilderness, group (Barrows/DKs/Moons), skilling, loot value |
| Task types | uniques, KC, pets, CAs, XP, slayer tasks, loot value |
| Always include | any catalog boss, sorted by how many clan members kill it |
| Lean towards | anything · what our members already do · things we rarely do |

Every preview row can be **locked**, **rerolled** (a different task at the
same difficulty) or **removed**, and **Reroll unlocked** redraws the rest.
Nothing is written until the admin adds the set. Every task stays editable
afterwards.

## Sizing: estimated team-hours

Each candidate gets an estimated **team-hours** figure: expected hours of one
player's efficient play, which the team can split however it likes.

- **KC:** `N / kills_per_hour`.
- **Any unique:** `n / (p_any × kph)`, for n in 1..min(5, max(2, uniques)).
- **Specific unique / pet:** `1 / (rate × kph)`.
- **CA:** a flat estimate per tier (Easy 0.3 h up to Grandmaster 12 h).
- **XP:** `xp / typical XP rate`.
- **Slayer tasks:** 1 h each.
- **Loot value:** GP at 1.5m/h.

The event's **capacity** is `team size × days × daily hours`. Tier bands are
shares of capacity with absolute floors, so tiny events still get sensible
targets:

| Tier (stored key) | Upper edge | Floor |
|---|---|---|
| Easy (`air`) | 0.5% of capacity | 0.25 h |
| Medium (`water`) | 2% | 0.75 h |
| Hard (`earth`) | 5% | 2 h |
| Elite (`fire`) | 15% | 5 h |

Anything above Elite is left out. KC, XP, slayer and loot targets are sized
to the geometric middle of each band and rounded to numbers people actually
write (1-10, then 1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5 × 10ⁿ). The difficulty
keys are the existing board-game tiers, so generated tasks drop straight
into board-game roll pools.

## Data sources

`web_api/task_generator_catalog.py` assembles the catalog and caches it for
15 minutes:

- **Encounters:** 45 curated bosses and raids in `services/task_generator.py`
  (`ENCOUNTERS`). Each lists its drop-attribution NPC names, Clan Log section
  slugs, WOM metric and any rate overrides.
- **Kill rates:** WOM EHB (`get_ehb_rates_sync`), then `npc_ehb_rates`, then
  the curated fallback. The last two are flagged `estimated`.
- **Uniques and pets:** `clan_log_items`. Attributable rows are uniques;
  non-attributable rows are the pet.
- **Per-item rates:** `xenforo.dt_npc_loot`, `max(rarity × rolls)`.
  - Raids, Dagannoth Kings, Grotesque Guardians and Moons have conditional
    or scattered tables, so they use a curated `unique_rate` and get no
    "this exact item" tasks.
- **CA tiers:** `utils.ca_tasks.catalog_records`, matched to each encounter's
  monster names.
- **Clan activity:** distinct members with drops at each encounter in the last
  90 days. It reads `user_group_association` joined to
  `player_npc_hourly_totals` on the `(player_id, npc_id, date_hour)` index,
  which takes about 0.1 s for a 500-member clan. Clans over 5,000 members
  (the global group) are skipped. Cached for 30 minutes per group.

## Selection

`select()` is weighted random with a seed, so the same seed gives the same
board.

- Tier quotas come from the mix by largest remainder.
- Elite is filled first because its pool is the thinnest. When a tier runs
  dry, it borrows from the nearest tier and reports `shortfall`.
- Weights are normalized **per kind**, so 21 skills don't outnumber 45
  bosses. Kind shares: uniques 3 · KC 2.5 · XP 1 · CA 0.8 · pets 0.6 ·
  slayer 0.4 · loot 0.4. Each repeat of a kind is damped.
- Diversity: at most 2 tasks per boss (the second at ×0.25) and 1 per
  skill, slayer or loot family. Categories are damped as they repeat.
- "Always include" bosses are placed first and survive the content filter.
- Clan focus multiplies a boss's weight by its active-member share:
  familiar `0.25 + 3·share`, fresh `1.6 − 1.4·share`.
- Rerolls pass `only_tier`, `taken_keys` (the rest of the board, for
  diversity) and `exclude_keys` (everything recently seen).

The generate route validates every pick with `validate_task_payload` and
refills failures (up to 3 rounds). Anything it returns will save.

## API (`web_api/routes/event_task_generator.py`)

| Route | Purpose |
|---|---|
| `GET /events/{id}/tasks/generator` | Defaults from the event, vocabularies, encounters with `clan_players` |
| `POST /events/{id}/tasks/generate` | Preview (no writes) → `{seed, capacity_hours, bounds, total_hours, shortfall, tasks[]}` |
| `POST /events/{id}/tasks/bulk` | Create up to 100 tasks in one transaction; duplicate labels and invalid payloads come back in `skipped` |

All three are event-admin gated. Generated tasks are saved as the event's own
private tasks and never publish into the shared library.

## Web surfaces

`components/event-task-generator.tsx` (`EventTaskGenerator`) appears in three
places:

- **Event manager, Tasks tab:** a "Fill for me" button next to "From
  library". Hidden for loot sweeps and SOTW/BOTW.
- **Setup wizard, Tasks step:** open by default when a non-bingo event has
  no tasks, since that is where drafts stall.
- **Bingo designer:** "Fill the board for me" or "Fill N empty cells for
  me".
  - Runs in `mode="cells"`: rows become `new_task` cells, shuffled so
    difficulty spreads across the board, and autosave writes them through
    the existing `PUT /events/{id}/bingo`.
  - Cells with a label of their own stay free.

## Library tidy and starter templates

- `scripts/tidy_task_library.py` (dry run by default, `--apply` to write)
  soft-deletes public duplicates and presets that no longer validate.
  - For duplicates it keeps, in order: an automatic task over a manual-only
    one, then curated > legacy > group, then the oldest id.
  - It reports manual-only legacy presets for a human to decide on.
  - The 2026-09-24 dry run found 10 duplicates, 0 broken presets and 9
    manual-only presets.
- `scripts/seed_starter_templates.py` (dry run by default, `--apply` to
  write) builds four site-wide public templates from the generator with fixed
  seeds: Weekend bingo 5x5, Two-week bingo 7x7, PvM week and Skilling week.
- Fixed along the way: event templates now carry each task's `difficulty`.
  Board-game templates used to re-run with empty tier pools.

## Deploy

1. Restart `droptracker-webapi`. There's no DDL and no worker or bot change.
2. Deploy the web app with `scripts/deploy.sh`, or
   `systemctl restart droptracker-node`.
3. Optional, owner's call: `venv/bin/python -m scripts.tidy_task_library --apply`
   and `venv/bin/python -m scripts.seed_starter_templates --apply`.

The web half calls routes the old backend doesn't have, so restart webapi
**before** the web deploy. If the web deploys first, the panel shows a load
error until webapi restarts.

## Later

- **PB tasks:** size them from our own PB spread (median and p25 per boss
  from `personal_best`).
- **Clue tasks:** `CLUE_TIERS` already has completion rates; they need
  casket item names.
- **Catalog coverage:** Yama, Doom, Royal Titans and Colosseum have KC and
  CAs but no uniques (no Clan Log section). Adding sections gives them
  unique tasks automatically.
- **Tuning:** check the tier bands against real completion rates once a few
  generated events have finished (`web_event_progress`).
- **Drop Four / Conquest:** the new event kinds under discussion can use the
  same catalog. A Drop Four column is a repeatable generated task, and a
  Conquest tile is an encounter.
