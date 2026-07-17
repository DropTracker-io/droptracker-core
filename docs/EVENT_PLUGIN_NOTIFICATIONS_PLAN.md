# Event → Plugin In-Game Notifications & Event HUD — Design Plan

Status: ALL PHASES BUILT 2026-07-17.
       P0 DEPLOYED (disc 045beb9 + web55a; api/events/webhook-consumer restarted).
       P0.5 DEPLOYED (disc f3f7365: /event_state, focus stamp, board.png, icons).
       P1 BUILT (disc 4c5bbc0 webapi routes DEPLOYED; web f582015 UI awaits next
       site deploy via `systemctl restart droptracker-node`).
       P2 BUILT on plugin branch `event-notifications` (e8993f8, off remove-lwgjl)
       — hub release rides the next plugin pin bump.
Scope: disc (intake API + event engine + notification fan-out), web (per-type preference UI),
       plugin (polling, rendering, config, HUD overlay, Events side-panel tab)

## Goal

Event participants who have the API enabled get close-to-real-time in-game
feedback and status for their events:

1. **Notifications** — chat messages / small text pop-ups when something
   event-relevant happens (teammate completes or progresses a task, lead
   changes, board turn ready, ...).
2. **Enhanced Display (HUD)** — a movable, persistent overlay showing the
   player's current event at a glance: task being worked toward (+item icon),
   team name/color (+icon), progress, placement.
3. **Events side-panel tab** — richer standing info in the existing plugin
   side panel: the player's active events, team placements, and an openable
   server-rendered board view with a team switcher.

## Non-goals

- No persistent connections in v1 (SSE/WebSocket is a possible later upgrade;
  everything below is transport-agnostic, so that stays additive).
- No server-driven rendering. The API sends typed data (and image URLs
  restricted to our domains); it never sends markup, format strings, or
  anything the client "executes". See Safety Contract.
- No client-side board rendering. Boards are server-rendered images (the
  plugin already displays server-generated lootboard PNGs with a pop-out
  dialog — `PanelElements.loadLootboardForGroup` / `showLootboardForGroup`).

## Architecture

```
event_engine → notification_queue (existing)
     │  └── plugin fan-out (P0, live): roster resolve + pref filter
     │        → RPUSH plugin:notify:{player_id}   (cap 50, TTL 24h)
     │  └── focus stamp (P0.5): plugin:focus:{player_id}:{event_id} = task_id
     │
webhook_consumer → submission_notice → same inbox (P0, live)
     │
plugin (10s poll while active_event) ─ GET /notifications  → drain inbox (P0, live)
                                     ─ GET /event_state    → HUD/tab state (P0.5)
                                     ─ GET /events/{id}/board.png?team_id= (P0.5)
```

### Delivered in P0 (live since 2026-07-17)

- `services/plugin_notifications.py` — typed-envelope per-player Redis inbox,
  team/event audience fan-out (dedicated read session so a failed read can
  never poison the event-apply transaction), web-pref filtering (fail-open),
  drain.
- Engine hook in `_enqueue_notification` ahead of the Discord
  `message_config` mute gate — in-game notifications are independent of the
  event's Discord verbosity.
- `GET /notifications` (intake API): drains the inbox; identity =
  `player_name` + `acc_hash`, hash-first (same trust model as `/load_config`);
  response also carries `active_event`.
- `/panel_data` group configs carry `"active_event"` (analogue of
  `track_xp_events`) so the plugin only polls during live events.
- `webhook_consumer` delivers processor `notice` text as `submission_notice`
  entries — restores the in-game chat channel queue mode silenced.
- `player_notification_prefs` table (web55a) for the P1 website prefs.

## State endpoint (P0.5): GET /event_state

The HUD and Events tab need *state* (what is my task/standing right now), not
just notification deltas — a login mid-event must populate instantly. Same
auth as `/notifications`. Response:

```json
{
  "events": [
    {
      "event": {"id": 19, "name": "Summer Bingo", "kind": "bingo"},
      "team": {"id": 38, "name": "Team Alpha", "color": "#cc4444",
               "icon_url": null, "score": 120, "rank": 2, "team_count": 4},
      "focus_task": {"id": 512, "label": "Obtain 3 Bandos hilts",
                     "icon_item_id": 11804, "have": 2, "need": 3,
                     "source": "inferred"},
      "board": {"image_url": "https://api.droptracker.io/events/19/board.png?team_id=38&...",
                "available": true},
      "standings": [{"team_id": 38, "name": "Team Alpha", "score": 120, "rank": 2}, ...]
    }
  ]
}
```

All composition logic is server-side so the client stays a dumb renderer:

- **`focus_task` (what the player is working toward).** Primary: an
  *inferred focus* — at apply time, when a player's own submission advances a
  task, stamp `plugin:focus:{player_id}:{event_id}` = task_id (Redis, TTL a
  few hours). The engine already knows the matched task at that moment, so
  this costs one SETEX on an existing code path — no extra matching work.
  Fallback when no stamp exists: the team's most-progressed incomplete task.
  `source` says which ("inferred" | "team_progress"). A later refinement can
  infer from non-matching activity too (e.g. the NPC of recent drops vs task
  sources) — deliberately deferred; the stamp covers the common case free.
- **Board-game events**: `focus_task` is the current tile's task; `board`
  carries the existing server-rendered board image.
- **`standings`**: teams ordered by score (the same ranking the engine
  already computes for completion payloads).
- Multi-event: one array entry per active event the player is rostered in.
  Which one the HUD shows is a client concern (pinned event, below).

**Board image route.** `GET /events/{id}/board.png?team_id=&player_name=&acc_hash=`
on the intake API: validates the caller is rostered in the event, then serves
the existing board render (same generator the team-channel board posts use),
cached by board state hash. `image_url` in the payload always points at our
API host.

## Safety contract (no code from server to client)

Unchanged from P0, extended for images:

- Typed envelopes only: `{id, type, ts, event?, data}`; hardcoded client
  registry maps `type` → renderer; unknown types dropped silently (forward
  compatible). Renderers compose display text locally from typed fields.
  `submission_notice` (server text) renders as sanitized plain chat only.
- **Icons**: task icons are sent as `icon_item_id` ints — the client renders
  the sprite locally via RuneLite's `ItemManager` (zero remote fetches).
  Team/event icons are `icon_url` strings; the client renders them only if
  the URL parses and its host is an allow-listed DropTracker domain
  (`droptracker.io` / `www.` / `api.`), decodes via `ImageIO.read` (returns
  null for non-image bytes — HTML/error pages can never render), and caps
  dimensions/bytes. This is the exact pattern the hub-approved plugin already
  ships for lootboard PNGs (`PanelElements.loadLootboardForGroup`).

## Notification types (unchanged from P0)

| type                    | default | controlled where        | audience        |
|-------------------------|---------|-------------------------|-----------------|
| event_task_completion   | on      | website pref            | whole team      |
| event_task_progress     | on      | **client toggle only**  | whole team      |
| event_lead_change       | on      | website pref            | all participants|
| event_board_turn / roll_prompt | on | website pref          | whole team      |
| event_started / event_ended | on  | website pref            | all participants|
| submission_notice       | on      | client (`receiveInGameMessages`) | submitter |

P0.5 payload enrichment: completion/progress fan-out gains `icon_item_id`
(the task icon the Discord embeds already resolve) so pop-ups/HUD can show
the item sprite.

## Multi-event & stacking rules

- **Pinned event (HUD).** The HUD shows ONE event at a time. The Events tab
  has a "show on HUD" picker; default = most recently activated event the
  player is in. Stored client-side (config), no server round-trip.
- **Batch grouping.** The plugin processes each poll's envelopes as one
  batch, grouped by `event.id`: at most one pop-up per event per batch
  (summarizing N items: "2 tasks progressed — Bandos hilt, Zamorak hilt");
  chat lines collapse beyond 3 per event per batch ("…and 2 more").
- **One drop, many tasks.** The server intentionally enqueues one envelope
  per (task, event) — cross-event and multi-task credit are distinct facts.
  Deduplication is presentation-side via the batch grouping above.
- **State refresh.** At most one `/event_state` refetch per poll batch that
  contained event-scoped types (debounced) — never per envelope.
- **Seen-id guard.** Client keeps a small LRU of processed envelope ids for
  the session; replays (e.g. a retried poll) render once.
- Known server-side limitation (accepted): a revoke → re-completion can
  produce a second notification for the "same" completion. Arguably correct —
  it did re-complete.

## Configuration split (no duplicated options)

**Plugin config (client, coarse — client always wins):**

1. `eventNotifications` (master toggle, default on; requires `useApi`).
2. `eventDisplayMode` (enum, intuitive names):
   - `CHAT` — "Chat messages only"
   - `POPUP` — "Chat + text pop-ups"
   - `ENHANCED` — "Enhanced display (HUD + pop-ups)"
3. `eventTaskProgressNotifications` (toggle, default on) — the high-volume
   type stays client-muted (never a website pref).
4. `eventHudDetail` (enum: `COMPACT` — task + progress bar; `DETAILED` —
   adds team name/rank and icons). Only meaningful in `ENHANCED`.

(The pinned-event choice is panel UI state persisted via config, not a
settings-panel option.)

**Website (fine-grained, per player):** per-type toggles for every type
EXCEPT `event_task_progress`, on the existing player settings surface —
enforced server-side at delivery, so a disabled type never enters the inbox.

## Client surfaces (P2)

- **Text pop-up**: lightweight toast overlay (top-center, fade ~6s, max 3
  stacked, batch-grouped per event).
- **Enhanced Display HUD**: a RuneLite `OverlayPanel` (movable/snappable for
  free via the overlay system — no custom drag code). Medium footprint:
  roughly infobox-row height ×2. Compact: task icon + label + progress bar.
  Detailed: + team color accent, team name, rank ("2nd of 4"). Data comes
  from `/event_state`, refreshed per the stacking rules; progress also
  advances optimistically from received envelopes between refreshes.
- **Events side-panel tab**: added to the existing Home | Activity | Player |
  Group panel. Lists each active event: name, team, rank, focus task,
  standings list, "show on HUD" picker, and a board thumbnail that opens the
  existing pop-out image dialog (reuse the lootboard dialog machinery) with a
  team dropdown that swaps the fetched `board.png?team_id=`.

Hub-review posture: no new permission surface — same API host, same
remote-image pattern already shipped for lootboards, sprites via ItemManager,
overlays via the standard overlay system. The biggest new client code is the
HUD panel and the Events tab layout, both plain Swing/overlay code.

## Extensibility (adding future notification types)

Unchanged: new needs are new `type` strings + typed fields; unaware plugins
drop them; renderers for unsent types are inert. The state endpoint is
likewise additive — new keys in `event_state` entries are ignored by older
clients. Never mutate existing types/fields.

## Server prefs storage

`player_notification_prefs` (live): one row per player, `prefs` JSON object
mapping type → bool; absent row/keys = enabled. Sink reads with a short-TTL
in-process cache if volume ever warrants (not needed at current scale).

## Build phases

- **P0 — DONE, DEPLOYED 2026-07-17**: inbox, fan-out, `GET /notifications`,
  `active_event` flag, `submission_notice` restore, prefs table.
- **P0.5 — DONE, DEPLOYED 2026-07-17**: `GET /event_state` (+ focus stamp at
  apply time, standings composition, board image route with roster auth),
  `icon_item_id` in completion/progress fan-out payloads, uniform team_name
  envelope enrichment.
- **P1 — DONE 2026-07-17**: webapi routes deployed; the settings-page UI is
  committed (web f582015) and goes live on the next site deploy.
- **P2 — DONE 2026-07-17** on plugin branch `event-notifications` (e8993f8):
  config (master, display mode, progress toggle, HUD detail), poll loop +
  batch grouping + seen-id LRU, type→renderer registry, toast overlay, HUD
  `OverlayPanel`, Events side-panel tab (standings + pinned picker + board
  pop-out via the lootboard dialog machinery), wire-format tests pinning the
  live JSON. Ships on the next hub pin bump.
- **P3**: defaults tuning after first live event; SSE upgrade if ~10s polling
  feels slow; NPC-activity-based focus inference refinement.
