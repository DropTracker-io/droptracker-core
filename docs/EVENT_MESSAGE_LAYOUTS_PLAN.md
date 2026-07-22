# Event Message Layouts — Group Customization Plan

Status: PROPOSED (investigation 2026-07-22). Goal: let groups configure the Discord
message layouts used for all event messaging on the website — a group-level default
set (part of group config, alongside the existing notification-embed editor) plus
optional per-event overrides — resolving per-event → group default → global default.

## What already exists (do not rebuild)

The web41a messaging overhaul (2026-07-14) already shipped most of the backend:

- **DSL + renderer**: `services/event_message_layouts.py`. Layout =
  `{"accent_color": "#RRGGBB"?, "blocks": [...]}` with block types `text`, `section`
  (text + thumbnail accessory), `separator`, `standings` (limit 1–25, medal list fed
  by sender-provided standings), `buttons` (URL buttons + Activity launch buttons).
  Token substitution is per-line: `{token}` resolved from
  `notification_context()`; any line with an unresolved token is dropped, empty
  blocks dropped, separators collapsed. `render_message_spec()` is pure/unit-tested.
- **Storage**: `web_event_message_layouts` (`EventMessageLayout`,
  `db/models/events.py:908`), unique on `(group_id, message_type)`. Columns:
  accent_color, layout (JSON text), schema_version, updated_at.
- **Resolution**: `load_layout(session, group_id, message_type)`
  (event_message_layouts.py:458): group row **only if `has_custom_embeds(group_id)`**
  → group-1 row → `DEFAULT_LAYOUTS` code default. So the entitlement story and the
  group→global fallback already work.
- **Message types**: `EVENT_MESSAGE_LAYOUT_TYPES` (db/models/events.py:243) — 20
  types = the 16 toggle keys + `event_signup_prompt`, `event_board`, `event_pot`,
  `event_board_win`. (`event_multi_clan_skipped` has a code default but is NOT in
  the list — decide: add it, or keep it code-only.) `event_board_win` is layout-only
  (queued as `event_board_turn` + `data.won`, remapped at render).
- **Send sites**: `notification_service.py:1929` (`render_event_components`, all
  queued types; falls back to legacy `build_event_embed` on render error — bad
  layouts already fail safe) and `event_board.py:248` (`event_board`). The team
  welcome board (`event_team_discord_bot.py:540`) is hard-coded inline — out of
  scope (could become a 21st type later).
- **Seeds**: `scripts/seed_event_message_layouts.py` upserts the 20 group-1 rows
  from `DEFAULT_LAYOUTS` (idempotent; `--force` clobbers staff edits).

**What's missing** — exactly the user-facing half:
1. No web_api routes to read/write layouts (grep confirmed: zero).
2. No web UI anywhere (no api-types schema, no client method, no component).
3. No per-event dimension (table keyed group+type only; `web_events.message_config`
   is toggles/verbosity only, no layout key).

## Design decisions

- **Entitlement**: keep piggybacking on `custom_embeds` (that is what `load_layout`
  already gates on, and it's already sold on t2/t3 as "custom embeds"). No new key.
  DELETE (reset-to-default) needs no entitlement, mirroring `routes/embeds.py` so
  downgraded groups can clean up.
- **Per-event storage**: extend `web_event_message_layouts` with
  `event_id INT NOT NULL DEFAULT 0` (0 = group-level default row), and replace the
  unique index with `(group_id, message_type, event_id)`. **Do NOT use nullable
  event_id**: MySQL unique indexes permit unlimited NULL duplicates (the exact bug
  behind the duplicate-group-association incident). No backfill needed — existing
  rows get 0.
- **Resolution order** (new): `(group_id, event_id)` → `(group_id, 0)` →
  `(1, 0)` → code default. Group-scoped candidates still require
  `has_custom_embeds(group_id)`. For CvC events each participating clan's messages
  resolve against *that clan's* group_id, so a host's per-event override applies to
  rows written under the host group; keep overrides host-scoped v1 (write rows with
  the event's host `group_id`; global events use group_id 1 + superadmin).
- **Per-event overrides are separate rows, not message_config keys**: layouts are
  large JSON; stuffing them into `web_events.message_config` would bloat every
  config round-trip and lose the unique-row upsert/audit pattern.
- **Editor meta comes from the backend**, not hardcoded in web (unlike
  embed-editor.tsx's PLACEHOLDERS): 20 types × a large token vocabulary is too much
  to duplicate and will drift. Add a `TOKEN_DOCS` registry next to `DEFAULT_LAYOUTS`
  (token, help, sample value, which types provide it) plus per-type capabilities
  (`supports_standings`, `supports_launch_button`, group label). Serve via one meta
  endpoint; web renders docs + drives sample-data preview from it.
- **Preview is client-side**, mirroring `render_message_spec` semantics (per-line
  token drop, empty-block drop, separator collapse, V2 container with accent bar) —
  same approach as the existing `DiscordPreview` in embed-editor.tsx, just for
  Components-V2 chrome. No server render endpoint needed for v1.

## Backend work (disc repo)

1. **Migration** (next webNNa): add `event_id` to `web_event_message_layouts`
   (NOT NULL DEFAULT 0), drop `uq_web_event_msg_layout`, create unique
   `(group_id, message_type, event_id)`.
2. **`load_layout(session, group_id, message_type, event_id=None)`**: candidate
   list as above; thread `event_id` through `render_event_components` and both
   callers (notification payloads and event_board both know the event id).
3. **Validator** `validate_layout_spec(layout) -> list[str]` in
   `event_message_layouts.py` (stdlib-only so unit tests keep working): block-type
   whitelist, limits (≤ ~15 blocks, content ≤ 2000 chars, ≤ 5 buttons/row, standings
   limit 1–25, `#RRGGBB` accent), URL check with `{token}` exemption (reuse the
   `_optional_url` idea from embeds.py — placeholder-bearing URLs skip scheme check;
   this exact class of bug 422'd the embeds editor once).
4. **New blueprint `web_api/routes/event_layouts.py`** (mirror embeds.py exactly —
   permissions, audit log, template-group handling):
   - `GET /groups/{id}/event-layouts` → `{layouts: [{message_type, custom, default}]}`
     (default = group-1 row else code default), + `GET /event-layouts/meta`
     (types, group labels, per-type tokens/samples/capabilities, block schema).
     Read = `assert_group_admin` (allow event managers? see per-event routes).
   - `PUT /groups/{id}/event-layouts/{message_type}` — group admin +
     `assert_group_entitlement(custom_embeds)`; group 1 = superadmin (this replaces
     hand-editing seeds: staff edit global defaults in the same UI).
   - `DELETE /groups/{id}/event-layouts/{message_type}` — group admin, no
     entitlement; 422 on template group.
   - Per-event: `GET /events/{id}/layouts`, `PUT/DELETE
     /events/{id}/layouts/{message_type}` — gate like `event_discord.py` PUT
     (group admin OR event manager) + `custom_embeds` on the resolved group;
     rows written with the event's host group_id + event_id. GET returns
     `{message_type, override, effective}` so the UI can show "overridden" badges
     and seed the editor from the effective group layout.
   - AuditLog actions `event_layouts.update` / `.reset` with before/after JSON.
5. Note deploy gotcha from web41a: web_api must not import `services.*` at module
   level (unit-test conftest stubs `services`) — lazy-import
   `event_message_layouts` inside handlers, as `event_discord.py` does.

## Web work (web repo)

1. **api-types**: `EventLayoutBlockSchema` (discriminated union on `type`),
   `EventMessageLayoutSchema`, `EventMessageLayoutInputSchema`,
   `EVENT_MESSAGE_LAYOUT_TYPES` + labels/groups, meta response schema.
2. **lib/api.ts**: `groupEventLayouts`, `saveGroupEventLayout`,
   `deleteGroupEventLayout`, `eventLayouts`, `saveEventLayout`,
   `deleteEventLayout`, `eventLayoutMeta` (+ mocks).
3. **Editor component** `components/event-layout-editor.tsx` (shared by both
   surfaces): type selector grouped (Lifecycle / Progress / Board game / Loot
   sweep / Leaderboard & pot / Admin) with "customized" dots; block list editor
   (add/remove/reorder; per-block mini-forms; standings block offered only when the
   type supports it per meta); accent color; click-to-copy token docs from meta;
   live V2-style preview with "fill sample data" toggle; Reset to default. Save via
   server actions returning `{ok, data|error}` (the embeds actions.ts style —
   thrown Server Action errors are redacted in prod).
4. **Group-level surface**: keep the existing Embeds tab but make
   `groups/[id]/embeds` a two-pane surface (segmented control: "Notifications" |
   "Event messages") rather than adding a 13th tab; `FeatureGate custom_embeds`
   unchanged. Superadmin edits group 1 via the same page (as embeds does today).
5. **Per-event surface**: new `CollapsibleSection` "Message layouts" in
   `components/event-discord.tsx` below Message verbosity — lists types with
   override badges, opens the shared editor seeded from `effective`, per-type
   "Revert to group default". This automatically appears in the event manager's
   Discord tab, the setup wizard's Discord step, and both standalone /discord pages.
   For CvC, show it only at shared/host scope (scope === null), matching where
   event-level knobs already live.
6. **Docs**: extend the `events-discord` CMS page with a "Customizing message
   layouts" section (docs authoring mechanism per event-docs memory).

## Phasing

- **P0 backend**: migration + load_layout event_id + validator + routes + tests
  (extend `tests/unit/test_event_message_layouts.py`; embeds-style route tests).
- **P1 group editor**: api-types + client + editor component + embeds-page split.
- **P2 per-event overrides**: event-discord section + event routes wiring.
- **P3 polish**: add `event_multi_clan_skipped` to the type list (+seed), consider
  team-welcome layout as a type, per-clan (non-host) event overrides if requested.

## Risks / gotchas

- Seed script `--force` clobbers group-1 edits — once staff edit defaults via the
  web, treat `--force` as forbidden in prod (or make it require `--types`).
- Renderer already falls back to the legacy embed on exceptions, so a bad saved
  layout degrades, not breaks — but the validator should keep users out of that.
- Entitlement cache is 60s TTL; layout reads happen on the bot hot path via
  `load_layout` per message — the extra `(group,event)` candidate adds one indexed
  row to the same single query, negligible.
- V2 components forbid `content=`/`embeds=`; pings render as a leading TextDisplay
  — the preview should show ping text the same way so WYSIWYG holds.
