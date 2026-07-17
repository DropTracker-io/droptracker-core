# Event → Plugin In-Game Notifications — Design Plan

Status: DRAFT (investigated 2026-07-17, not yet approved for build)
Scope: disc (intake API + notification service), web (per-type preference UI), plugin (polling, rendering, config)

## Goal

Event participants who have the API enabled receive close-to-real-time in-game
feedback (chat message and/or mini popup) when something event-relevant happens —
e.g. a teammate's drop completes or progresses one of their team's tasks.

## Non-goals

- No persistent connections in v1 (SSE/WebSocket is a possible v2 upgrade; the
  design below is transport-agnostic so it stays additive).
- No server-driven rendering. The API never sends markup, format strings, or
  anything the client "executes". See Safety Contract.

## Current-state facts this design builds on

- The event engine already enqueues typed, display-ready rows into the
  `notification_queue` table (`services/event_engine.py` `_enqueue_notification`,
  ~:952): `event_completion`, `event_task_progress`, `event_lead_change`,
  board-game turn prompts, etc. Single consumer today: `services/
  notification_service.py` (core bot) → Discord.
- Plugin↔server is pure HTTP polling. The plugin polls batch `POST /check` every
  10s (ActivityPanel Swing timer) and `GET /panel_data` on a 60s TTL.
- The old `/webhook` response `notice`/`rank_update` chat channel is dead since
  the queue-mode flip (acceptor returns `Queued`; consumer drops notices). This
  plan restores that functionality as a side effect.
- Identity = `player_name` + `acc_hash` (no token). The notification channel
  inherits this trust level; content is non-sensitive.
- `GroupConfig.track_xp_events` is the precedent for the server telling the
  plugin to change polling behavior during events.

## Architecture (v1: per-player inbox + polling)

```
event_engine → notification_queue (existing, unchanged)
                     │
        notification_service (existing consumer)
                     ├── Discord sinks (existing, unchanged)
                     └── NEW plugin sink:
                         resolve team roster → filter by web prefs +
                         API-user status → RPUSH plugin:notify:{player_id}
                                                     │
plugin (10s poll while event active) ← GET /notifications (intake API, Quart)
```

### Redis inbox

- Key: `plugin:notify:{player_id}` (LIST). `LTRIM` to 50, TTL 24h refreshed on
  push. Drained with LPOP batch on read (at-most-once is acceptable for
  ephemeral toasts; a missed one is visible on the event page anyway).
- Fan-out happens in the plugin sink, not in the event engine — the
  `notification_queue` row carries only the *acting* player; the sink expands to
  the full team roster (same roster resolution the Discord path already uses)
  and writes one inbox entry per recipient that passes filters.

### New endpoint (intake API, `api/routes/`)

`GET /notifications?player_name=<name>&acc_hash=<hash>`

- Resolves player exactly like existing endpoints (`ensure_player_and_auth`
  semantics — hash must match the stored `Player.account_hash`).
- Response: `{"notifications": [ ... ]}` (drained), plus `"active_event": bool`
  so the plugin can stop polling when the event ends without waiting for the
  next `/panel_data` refresh.
- Rate-limited like `/check`. Cheap: one Redis LPOP pipeline, no MySQL on the
  hot path.

### Plugin activation signal

`/panel_data` group-config payload gains `"active_event": true` (exact analogue
of `track_xp_events`) whenever the player is on a roster of a live event. The
plugin polls `/notifications` on its existing 10s timer only while this is true
(and `useApi` + the client master toggle are on).

## Safety contract (no code from server to client)

The API sends **typed data only**. Every notification is:

```json
{
  "id": "1752770000-42",
  "type": "event_task_completion",
  "ts": 1752770000,
  "event": {"id": 12, "name": "Summer Bingo"},
  "data": {
    "task": "Obtain 3 Bandos hilts",
    "team": "Team Alpha",
    "acting_player": "SomeTeammate",
    "item": "Bandos hilt",
    "points": 5,
    "progress": "3/3"
  }
}
```

Client-side rules (plugin):

- A hardcoded registry maps `type` → renderer (an `EnumMap`). **Unknown `type`
  values are dropped silently** — forward compatibility without ever rendering
  unrecognized content.
- Each renderer composes its display text **locally** from the typed fields
  ("[DropTracker] Team Alpha completed: Obtain 3 Bandos hilts (+5 pts)").
  The server never supplies a full display string for these.
- All string fields pass the existing `sanitize()` (tag-strip) before use, and
  are length-capped.
- The one legacy exception: `type: "submission_notice"` (restoring the dead
  `notice` channel) carries server text; it is rendered as plain sanitized text
  in chat only, gated by the existing `receiveInGameMessages` config, exactly
  as the old code path did.

## Notification types (v1 set)

| type                    | default | controlled where        | typical audience        |
|-------------------------|---------|-------------------------|-------------------------|
| event_task_completion   | on      | website pref            | whole team              |
| event_task_progress     | on      | **client toggle only**  | whole team              |
| event_lead_change       | on      | website pref            | all participants        |
| event_board_turn        | on      | website pref            | whole team (board game) |
| event_started / event_ended | on  | website pref            | all participants        |
| submission_notice       | on      | client (`receiveInGameMessages`, existing) | submitting player only |

`event_task_progress` is the one type controlled on the client instead of the
website (it's the highest-volume type, so the mute switch belongs where the
noise is felt — in game). It is always delivered to the inbox for API users;
the plugin filters it at render time. It does NOT appear in the website prefs
(no option exists in two places).

## Configuration split (no duplicated options)

**Plugin config (client, coarse — client always wins):**

1. `eventNotifications` (toggle, default on; only functions with `useApi`) —
   master on/off for all event notification types.
2. `eventNotificationStyle` (enum: `CHAT`, `POPUP`, `CHAT_AND_POPUP`) — one
   style choice applied to every event notification.
3. `eventTaskProgressNotifications` (toggle, default on) — render teammate
   task-progress notifications (the high-volume type; see table below).

Nothing else client-side. Existing `receiveInGameMessages` keeps governing
submission confirmations (unchanged meaning).

**Website (fine-grained, per player):**

- Per-type on/off toggles for every type EXCEPT `event_task_progress`
  (client-owned; table above defines defaults), on the existing player
  settings surface.
- Stored server-side; enforced at **enqueue time** in the plugin sink, so
  disabled types never even enter the inbox.

**Precedence model:** website prefs decide *what is delivered*; client config
decides *whether and how anything renders*. Client master toggle off ⇒ plugin
doesn't poll at all. No setting exists in both places.

## Popup rendering (plugin)

- `CHAT`: existing `ChatMessageUtil` path.
- `POPUP`: lightweight overlay toast (custom `Overlay`, top-center, fade after
  ~6s, stacked max 3) — an in-client "mini pop-up", not the OS-level RuneLite
  `Notifier`.

## Extensibility (adding future notification types)

The channel is deliberately generic — nothing about the envelope, inbox,
endpoint, or transport is event-specific. Adding a new notification type
(event-related or not: group announcements, system notices, moderation
outcomes, ...) touches exactly three places and requires **no changes to the
transport, endpoint, inbox schema, or existing renderers**:

1. **Server producer**: push an entry with the new `type` string and its typed
   `data` fields into the recipient's inbox (via the shared push helper).
2. **Web pref row** (optional): register the type in the per-type preference
   list so users can opt out; unregistered types follow their hardcoded
   default.
3. **Plugin renderer**: add one entry to the client's type→renderer registry
   in a plugin release.

Ordering between 1 and 3 is safe in both directions: plugins that don't yet
know a type drop it silently (server can ship first), and renderers for types
the server never sends are inert (plugin can ship first). The envelope
(`id`/`type`/`ts`/`data`) is versionless by design — new needs are expressed as
new types, never by mutating existing ones.

## Server prefs storage

Per-user JSON or columns keyed by `player_id` (implementation detail for build
phase; candidates: extend existing player-settings storage used by the web
settings page). Sink reads with a short-TTL in-process cache to keep the
notification loop cheap.

## v2 (optional, additive)

SSE endpoint on the intake API (Quart natively supports streaming; model on
`web_api/routes/realtime.py`), authenticated by name+hash, subscribed to the
same inbox via `rt:player:{id}` pub/sub mirroring. Drops latency from ~10s to
sub-second without changing the contract, filters, or config model.

## Build phases (when approved)

- P0 disc: plugin sink in notification_service (roster fan-out + pref filter +
  inbox), `GET /notifications`, `active_event` flag in `/panel_data`,
  `submission_notice` enqueue from webhook_consumer (restores dead channel).
- P1 web: per-type notification preferences UI on player settings + API route.
- P2 plugin: config additions, poll loop, type registry + renderers, toast
  overlay. (Plugin-hub release rides the next pin bump.)
- P3: defaults tuning after first live event; consider SSE if latency feedback
  warrants.
