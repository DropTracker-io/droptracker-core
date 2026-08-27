# Event Team Indicators in Clan Chat — Design Plan

Status: **BUILT 2026-08-27** — P1, the orb sprites from P2, and P4's public-chat
       toggle. Still open: item-sprite badges and the web team-icon picker for
       non-board events (rest of P2), clan broadcasts (P3), and the tag chip on
       public event pages. Three corrections found on implementation are noted
       inline below (migration slug, `setOnStateUpdated`, the Discord orb).
Scope: disc (intake API roster endpoint, `/event_state` version stamp, `short_tag`
       column + admin PATCH, manifest kill switch), web (team cosmetics editor,
       tag chip on public surfaces), plugin (roster cache, chat decoration,
       mod-icon slots, config).
Origin: user suggestion — *"For Events, indicate what team someone is on in Clan
       chat."* — referencing the Plugin Hub plugin **Bingo Team Indicators**
       (`github.com/EwwItsMike/BingoTeamIndicators`, hub manifest
       `plugin-hub/plugins/bingo-team-indicators`).

---

## Goal

While a player is in a live DropTracker event, every clan-chat line from another
participant of that event carries a small team badge, so you can read the room
during a bingo/CvC without cross-referencing the website.

The difference from the reference plugin is the whole point: **the roster comes
from the API, not from the user typing names into a side panel.** Nobody
maintains a list, teams stay correct when an admin moves someone, and it works
for every participant the moment they log in.

## Non-goals

- No manual/local roster entry. If the API is off or the player is not in an
  event, the feature is simply inert.
- No new hosts, no new client permissions. Sprites resolve through RuneLite's
  `ItemManager`; nothing is fetched that the plugin does not already fetch.
- No client-side parsing of clan broadcast text in v1 (see Phase 3 for the
  safe version of that).
- No leaking rosters of events the viewer is not in.

---

## How the reference plugin works (and what we take from it)

`BingoTeamIndicatorsPlugin` (193 lines) does three things:

1. **Sprite injection** — at `LOGGED_IN` it copies `client.getModIcons()` into a
   larger array, decodes 15 bundled PNGs (`one.png`…`fifteen.png`) into
   `IndexedSprite`s via `ImageUtil.getImageIndexedSprite`, and calls
   `client.setModIcons(...)`. Each slot yields a chat tag `"<img=" + idx + ">"`.
2. **Name→team map** — built from comma-separated names the user typed into a
   side panel (`PersistentVariablesHandler`), keyed by
   `Text.standardize(Text.removeTags(name)).toLowerCase()`.
3. **Decoration** — on every `CLAN_CHAT`/`CLAN_GUEST_CHAT`/`FRIENDSCHAT` message
   whose sender is in the map, it walks the **entire** `client.getMessages()`
   buffer and rewrites each matching `MessageNode`'s name to
   `iconTag + rsn`, then `client.refreshChat()`. For `CLAN_MESSAGE` broadcasts it
   splits the message on a hardcoded list of phrases
   (`" has achieved "`, `" received a "`, …) to recover the RSN and prefixes
   `MessageNode.setValue`.

**Keep:** the mod-icon slot mechanism, `MessageNode` rewriting + `refreshChat()`
(this is also RuneLite core's own clan-rank-icon pattern), and the
`Text.standardize` name key.

**Drop:** the manual roster, the full-buffer walk on every message (O(buffer)
per line; we decorate the incoming node and only re-walk when the roster
changes), and the hardcoded broadcast phrase list (brittle — see Phase 3).

---

## What we already have

| Piece | Where | Note |
|---|---|---|
| `GET /event_state` (one entry per active event the player is rostered in) | `api/routes/notifications.py:153`, composed in `services/plugin_notifications.py:905` | Already carries `team{id,name,color,icon_item_id,icon_path}`, `standings[]` (every team: id/name/score/rank/color) and `members[]` — but **own team only**, capped at `MEMBERS_LIMIT = 60` |
| Poll loop + state cache + "fresh state landed" callback | `service/EventNotificationService.java` (`refreshEventStateNow`, `setOnStateUpdated`, `hudEntry()`) | The roster consumer hangs off this; no new poll loop needed |
| Pinned-event tiebreak | `config.pinnedEventId()` → `EventNotificationService.hudEntry()` | Reuse for "which event's badge wins" |
| Mod-icon injection, correct lifecycle | `events/WidgetEventHandler.java:359` (`loadPets`) — allocate at `LOGIN_SCREEN`, reset index on `STARTING` | Copy this lifecycle exactly |
| Chat rendering helpers, sanitization | `util/ChatMessageUtil.java`, `EventNotificationService.clean()` | |
| Team cosmetics (name/color/piece item) end to end | `PATCH /events/{id}/teams/{team_id}` → `web_api/routes/events.py:5153`; zod at `packages/api-types/src/index.ts:4642`; UI at `components/event-manager.tsx:632-660` | The `short_tag` field rides these rails |
| Roster gating precedent | `board.png` gate, `api/routes/notifications.py:212` | Copy verbatim |
| Per-entity Redis cache precedent | `TEAM_SUBMISSIONS_KEY_TEMPLATE` (`plugin:team_subs:{team_id}`, 30s) | Same shape for the roster |
| Server kill switch precedent | `manifest.sync.enabled` → `Manifest.SyncSettings` | Same shape for this feature |

## The gap

`/event_state` tells a client **its own** team and the *names* of the other
teams. It never says **who is on the other teams**. Everything below exists to
close that gap without bloating a payload that is polled continuously.

---

## Server design (disc)

### 1. New endpoint — `GET /event_roster` (intake API)

Lives in `api/routes/notifications.py` beside `/event_state`; composition in
`services/plugin_notifications.py`. Identity is `player_name` + `acc_hash`,
hash-first, same as every other plugin endpoint. Roster-gated: an event the
caller is not on a team of is not in the response (and a `?event_id=` for such
an event 404s with the same body as "event not found" — never confirm a private
event exists).

```json
{
  "events": [{
    "event_id": 42,
    "roster_version": "3f1c9a0b",
    "teams": [
      {"id": 101, "name": "Red Rockets",  "short_tag": "RED", "color": "#cc3333", "icon_item_id": 11802, "icon_path": null},
      {"id": 102, "name": "Blue Blazers", "short_tag": "BLU", "color": "#3355cc", "icon_item_id": 4151,  "icon_path": null}
    ],
    "members": {"101": ["beast owned", "zezima"], "102": ["woox"]},
    "members_total": 3,
    "truncated": false
  }]
}
```

**Names are pre-normalized server-side** with
`utils.format.normalize_player_display_equivalence` (lower, `_`/`-`→space,
whitespace collapsed). That is the single most important detail in this document
— see *Trap 1*.

**Caps.** `ROSTER_TEAMS_LIMIT = 32`, `ROSTER_NAMES_LIMIT = 2000` per event
(oldest-joined first so the badge set is stable across truncation), `truncated`
tells the client it is looking at a partial map. A 3-clan CvC of 400-member
clans is ~1200 names ≈ 25 KB raw / ~6 KB gzipped — fine for a
version-gated fetch, **not** fine inside the continuous `/event_state` poll.

**Cache.** `plugin:event_roster:{event_id}`, TTL 300s, **keyed per event, not per
viewer** — the P0-14 lesson from `/event_state`: one completion wakes a whole
roster's clients at once and every one of them reads the identical bytes.
Rate limit `12/60s` to match `/event_state`.

**auto_clan teams.** Clan-vs-clan whole-clan teams carry no explicit roster rows
in principle, but `services/event_lifecycle.sync_auto_clan_rosters` materializes
`EventTeamMember` rows every sweep tick precisely so read surfaces work. Read
`EventTeamMember` like every other surface does; do **not** re-expand
`user_group_association` here.

**Hidden players.** `players.hidden` members are masked out of the map (they
just get no badge). That matches every other event read surface
(`web_api/routes/events.py:1584`). Cheap to flip if the owner prefers otherwise
— the audience is already roster-gated.

### 2. `roster_version` on `/event_state`

Add one additive string field per entry in `compose_event_state`:

```python
roster_version = sha1(f"{member_count}|{max_joined_at}|" + "|".join(
    f"{t.id}:{t.name}:{t.color}:{t.short_tag}:{t.piece_item_id}" for t in teams))[:16]
```

`teams` is already loaded there (it builds `standings` from it), so this costs
one extra `COUNT(*) , MAX(joined_at)` aggregate per event. Add/remove changes the
count; a same-count swap changes `max(joined_at)`; a rename/recolor/retag
changes the team tuple. The plugin refetches `/event_roster` only when this
string differs from what it holds.

Older plugins ignore the new key — the additive contract in
`EVENT_PLUGIN_NOTIFICATIONS_PLAN.md` ("Extensibility") already covers this.

### 3. `web_event_teams.short_tag`

Migration `alembic/versions/web103a_event_team_short_tag.py` (template:
`web101a_notification_blacklist.py`). **Correction:** the `web101a` slug this
plan first named was taken by `web101a_notification_blacklist` before the
feature was picked up — check `alembic heads` before choosing one.

```python
op.add_column("web_event_teams", sa.Column("short_tag", sa.String(8), nullable=True))
```

NULL means "derive one". Derivation is a pure function
(`services/event_teams.derive_short_tag`), so it unit-tests without a DB:
multi-word name → initials (≤4); single word → first 3 chars; uppercase;
deduped within the event by appending a digit. Deterministic, so the same event
always renders the same tags.

Wire it into `PATCH /events/{id}/teams/{team_id}` (`web_api/routes/events.py:5153`)
alongside `name`/`color`/`piece_item_id`: validate `^[A-Za-z0-9 ]{1,8}$`, allow
null to reset, include it in the existing `before`/`after` `AuditLog` row.
No `_sync_team_discord` re-pend needed — the tag has no Discord surface.

### 4. Manifest kill switch

`manifest.team_indicators.enabled` (default true), mirroring
`manifest.sync.enabled` / `Manifest.SyncSettings`. Plugin releases are gated on
Plugin Hub review; a server-side off switch means a misbehaving decoration can
be stopped in minutes instead of a hub round-trip.

---

## Plugin design

### New: `service/EventTeamIndicatorService.java`

Singleton, injected into `DropTrackerPlugin`. Holds:

- `Map<String, TeamBadge> byName` — normalized RSN → `{teamId, tag, color, iconSlot}`,
  rebuilt wholesale on each roster load (never mutated in place).
- `String rosterVersion` — last version fetched, per event id.
- Slot table `Map<Integer, Integer> teamIdToIconSlot`.

**Feeding it.** `EventNotificationService` fires off-EDT whenever a fresh
`/event_state` lands. **Correction:** that signal was a single
`@Setter` `Runnable` slot already owned by `DropTrackerPanel` (set on open,
nulled on close), so a second consumer would have silently killed the panel's
refresh. It is now a `CopyOnWriteArrayList` with
`addStateUpdatedListener` / `removeStateUpdatedListener`, and the panel holds
its own registration so a re-init deregisters before re-adding. Compare `roster_version` per
event; on mismatch (or first load) fetch `/event_roster` on the executor and
swap the map. No new poll loop, no new cadence.

Skip the fetch entirely when `config.eventTeamIndicators() == OFF`, when
`!config.useApi()`, or when the manifest switch is off — users who don't want
this pay nothing.

**Multi-event.** A player can be rostered in several live events at once, and a
clanmate can be on different teams in two of them. Resolve with the same
`hudEntry()` order the HUD uses (pinned event, else first) so the chat badge and
the HUD never disagree. Document it in the config tooltip.

### Chat decoration

Hook in `DropTrackerPlugin.onChatMessage` — **above** the `if (!isTracking)
return;` at `DropTrackerPlugin.java:486`. `isTracking` is the webhook-exhaustion
kill switch (`api/UrlManager.java:183`); it disables *submission* processing, and
gating a display feature behind it would silently kill badges for anyone whose
webhook list failed to replenish.

```java
// CLAN_CHAT, CLAN_GUEST_CHAT, CLAN_GIM_CHAT, FRIENDSCHAT
// (+ PUBLICCHAT behind its own opt-in config)
teamIndicators.decorate(message.getMessageNode());
```

`decorate` is one node, not a buffer walk:

1. `String rsn = Text.standardize(Text.removeTags(node.getName()))` — matches
   the server's normalizer, and `standardize` also folds the `\u00A0` the game
   uses for spaces in RSNs.
2. Bail if `ChatMessageUtil.isDiscordBridgeSender(node.getName())` — our own
   Discord→game bridge lines re-enter as real `ChatMessage`s.
3. Bail if the name already starts with our marker (idempotency; the retro-walk
   and the live hook can both reach the same node).
4. `node.setName(badge + node.getName())`, then `client.refreshChat()`.

Prefixing is safe next to the game's own clan-rank icons — the result is just
`<img=T><img=25>Name`.

**Retro-apply.** When a new roster lands or the config changes, walk
`client.getMessages()` **once**, decorate matching nodes, and call
`refreshChat()` once — on the client thread. This is the reference plugin's
rebuild, but triggered by roster change rather than by every incoming line.

### Badge rendering

Config enum `TeamIndicatorStyle { OFF, ICON, TAG, ICON_AND_TAG }`, default
`ICON`, where `ICON` degrades to the tag if no sprite slot could be allocated —
so Phase 1 can ship with zero sprite work and the default never has to change
later.

- **TAG** — `"<col=" + hex + ">[" + shortTag + "]</col>"` using the team's
  `color`. ~5 chars of chat width. `</col>` resets to the chat default rather
  than an enclosing tag (already documented in `ChatMessageUtil`), so it must be
  appended as a sibling, never nested.
- **ORB (shipped default)** — the colored circle the team's Discord channel
  already carries. `services/event_team_discord.py` maps an accent color to
  one of 🟢🔴🔵🟡🟠🟣⚪ through named hue bands to name team channels; the
  roster payload returns that circle plus its own Twemoji fill (`orb_color`),
  and the client draws a 12px disc in that fill. Chat has no glyph for an
  emoji, so it has to be a sprite — but routing it through the same bands is
  what makes the badge, the Discord channel icon and the site's team dot agree.
  This answers the suggestion's own follow-up ("probably use the same color orb
  as in discord?").
- **ICON (not built)** — a mod-icon slot per team from `team.icon_item_id` →
  `itemManager.getImage(id)` → `ImageUtil.resizeImage(img, 18, 16)` →
  `ImageUtil.getImageIndexedSprite(img, client)`. Exactly `loadPets`
  (`WidgetEventHandler.java:374-392`). No `icon_item_id` → generate a solid
  rounded chip from `team.color` in memory (no asset, any team count).

**Slot lifecycle — reserve once, refill in place.** Allocate
`MAX_TEAM_ICONS = 12` slots at `LOGIN_SCREEN` when the index is `-1`, and reset
the index on `STARTING` — the same switch `WidgetEventHandler` already uses,
because the client rebuilds the mod-icon array on restart. Never
`Arrays.copyOf` again on a roster change: rewrite `client.getModIcons()[slot]`
in place on the client thread. Growing the array per roster refresh leaks slots
and can stomp on icons other plugins appended after us. Teams past the budget
fall back to TAG.

### Config (Events section, `DropTrackerConfig.java` after `eventHudDetail`)

| key | type | default | note |
|---|---|---|---|
| `eventTeamIndicators` | `TeamIndicatorStyle` | `ICON` | Master + style in one control |
| `eventTeamIndicatorsPublicChat` | boolean | `false` | Also badge `PUBLICCHAT` (useful at a raid/boss during a CvC) |

---

## Web design

1. **Team cosmetics editor** — `components/event-manager.tsx`, the team row that
   already has rename (`onRenameTeam`, :632) and the color picker
   (`onColorTeam`, :649): add a **Chat tag** input (≤8 chars, placeholder =
   the derived fallback) plus a small live preview of the rendered chat line.
   Same optimistic-update-and-revert shape as the two beside it.
2. **Schema** — extend `EventTeamPatchSchema`
   (`packages/api-types/src/index.ts:4642`) with
   `short_tag: z.string().max(8).regex(/^[A-Za-z0-9 ]*$/).nullable().optional()`,
   and relax the `.refine` so a tag-only patch is valid.
   `lib/api/events.ts:826` passes it straight through.
3. **Team icon for non-board events** — `piece_item_id` is only surfaced today
   inside the board designer (`components/event-board-designer.tsx:1393`).
   Lift the same item picker into the team row for every event kind, relabelled
   *"Team icon (shown in game chat)"*. This is the biggest UX win on the web
   side: it is what makes the in-game badge distinctive instead of a color dot.
4. **Show the tag where teams are shown** — a small chip next to the team name
   on the public event page and Teams tab (`components/event-teams-board.tsx`,
   `event-team-view.tsx`, `event-standings-strip.tsx`) so the site and the game
   agree on what "RED" means.
5. Nothing needed on the player settings page — the style is a client config.
   Optionally one explanatory line near the existing event notification prefs.

---

## Traps

1. **RSN spelling divergence.** The plugin can submit `Beast_Owned` while the DB
   stores `Beast Owned`, and in-chat names use `\u00A0` (U+00A0) for spaces. Normalize on
   *both* sides (`normalize_player_display_equivalence` server-side,
   `Text.standardize` client-side) and never compare raw names with `==`.
2. **Name changes mid-event.** `players.player_name` goes stale until a WOM
   refresh, so a renamed clanmate silently loses their badge. Plugin users
   self-heal (every submission carries the live name, resolved by account hash);
   non-plugin clanmates do not. Accept as a known limitation in v1; a
   `player_aliases` history table is the real fix and is out of scope here.
3. **Mod-icon slots.** Allocate once per client lifetime, refill in place.
   See the lifecycle note above — this is the most likely source of a
   "someone else's plugin's icons went weird" bug report.
4. **Discord bridge echo.** `ChatMessageUtil.sendDiscordClanMessage` renders
   Discord lines through `chatMessageManager`, which posts a real `ChatMessage`
   back through our own subscriber. Skip `(Discord)`-marked senders.
5. **Payload growth.** Never put the roster in `/event_state`. Separate
   endpoint, per-event cache, version-gated refetch, hard caps.
6. **Privacy.** Roster-gated both ways: only rostered callers get a response,
   and only events they are rostered in appear. Hidden players masked.
7. **Buffer walk cost.** `client.getMessages()` is up to ~500 nodes; walk it on
   roster/config change only, never per message.
8. **Plugin Hub review posture.** No new hosts (`DropTrackerUrls` unchanged), no
   new permissions, sprites via `ItemManager`, the one new endpoint is on the
   existing API base. Worth stating in the hub PR description.
9. **`disc` test conftest stubs `db`/`services`.** Import services lazily inside
   the handler, exactly as `api/routes/notifications.py` already documents at
   the top of the file.

---

## Phases

- **P1 — text tags, no sprites.** `short_tag` column + migration + PATCH + web
  editor; `GET /event_roster`; `roster_version` on `/event_state`; plugin
  service + `CLAN_CHAT`/`CLAN_GUEST_CHAT`/`CLAN_GIM_CHAT`/`FRIENDSCHAT`
  decoration in TAG mode; config. Shippable and useful on its own.
- **P2 — icons.** Mod-icon slot allocator, item-sprite and color-chip badges,
  team icon picker for all event kinds on the web, `ICON` default.
- **P3 — clan broadcasts (optional).** Badge `CLAN_MESSAGE` lines
  ("*X received a drop: …*"). Do **not** copy the reference plugin's hardcoded
  phrase list. Instead: take progressively shorter leading prefixes of the
  message (RSNs are ≤12 chars, ≤3 words) and look each up in the roster map —
  longest match wins. No phrase list to maintain, no breakage when Jagex
  reworders a broadcast, and it degrades to "no badge" instead of a wrong one.
- **P4 — public chat** behind its own toggle.

## Tests

**disc** (`tests/unit/`)
- `test_plugin_event_roster.py` — response shape; name normalization; caller not
  rostered → 404 with the "event not found" body; caps + `truncated`; auto_clan
  teams resolve through materialized rows; hidden players masked.
- `test_event_team_short_tag.py` — `derive_short_tag` purity, dedup within an
  event, PATCH validation + audit row.
- `roster_version` stability: unchanged across two composes; changes on member
  add, member remove, and team rename/recolor/retag.

**plugin** (`src/test/java/io/droptracker/service/`)
- Wire-format test pinning the `/event_roster` JSON (precedent:
  `EventNotificationParseTest`).
- Pure-logic tests: name key equality across `Beast_Owned` / `Beast Owned` /
  `Beast\u00A0Owned`; badge resolution and multi-event pinned-event pick;
  idempotency (decorating twice yields one badge); Discord-sender skip; graceful
  behaviour when the roster is truncated or a team has no `short_tag`.

**web**
- `EventTeamPatchSchema` round-trip incl. tag-only patch and the relaxed refine.
