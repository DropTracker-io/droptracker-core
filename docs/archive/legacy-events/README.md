# Legacy events archive (2026-07-05)

Backup taken before decommissioning the legacy event systems (events-prd.md D2,
backend task 22). Kept because the BoardGame mechanics are the design reference
for the Phase-C board-game mode.

- `events_legacy_20260705.sql` — mysqldump (schema + data) of the legacy tables:
  `events`, `event_tasks` (57 rows, "Global Bingo #1"), `event_teams` (4),
  `event_participants`, `event_configurations`, `event_items`,
  `event_notifications`, `event_team_cooldowns`, `event_team_effects`,
  `event_team_inventory`, plus `bingo_boards`, `bingo_games`, `assigned_tasks`,
  `bingo_board_tiles`. Test-restored successfully on 2026-07-05.
- `legacy-events-code_20260705.tgz` — `disc/games/events/` (BoardGame system,
  dice/tiles/shop/effects), `disc/games/gielinor_race/`, `disc/eventBot.py`
  as they stood before deletion.

The curated task list from `games/events/task_store/default.json` lives on in
the `web_event_task_library` table (seeded by `scripts/seed_event_task_library.py`).
