"""Clan-broadcast submissions: tracking non-plugin clanmates from relayed chat.

The plugin's clan-relay feature (``relayClanBroadcasts``) forwards in-game
``CLAN_MESSAGE`` system broadcasts verbatim as ``type=clan_broadcast``
payloads. This processor turns the ones we track (item/raid/clue drops, pets,
collection log slots, personal bests — ``utils.clan_broadcasts.TRACKED_KINDS``)
into records for clanmates who do NOT run the plugin themselves. Everything about a chat
row is deliberately second-class next to a plugin submission:

- The RELAYER authenticates like any submitter (``ensure_player_and_auth``,
  hash-checked + rate-limited); the SUBJECT — the player the broadcast is
  about — binds only to an EXISTING roster row of a bound group. Names never
  mint players here; that stays with the WOM roster sync
  (``auto_provision_members``), whose stub-healing path then makes chat rows
  follow the player when they later install the plugin.
- A subject whose row carries a real (non ``wom_temp_``) account hash runs the
  plugin, and their own structured submission is strictly richer — but the
  plugin can miss a drop (client closed, plugin disabled, mobile). So instead
  of dropping the chat copy outright, it is RECONCILED: if a matching plugin
  submission already exists the copy is suppressed; otherwise the broadcast is
  parked in Redis (``clanchat:deferred``) for ``PLUGIN_GRACE_SECONDS`` and
  replayed by the webhook consumer's maintenance sweep — recorded only if the
  plugin copy STILL hasn't arrived. The grace window is what prevents the
  double-count race (broadcast relays beat the subject's own submission by
  seconds routinely; both directions are covered by deciding late).
- Rows are written ``authed=False``, ``source='clan_chat'`` — the single
  filter handle every surface can use. Events v2, group/global points and
  split credits are never fed from here (this processor simply never calls
  those hooks). Groups the subject belongs to that did NOT opt in get durable
  ``drop_group_moderation`` exclusions, exactly like manual-policy withholds,
  so board rebuilds keep chat drops off their boards too.

Group binding: a broadcast belongs to the groups OF THE RELAYER whose
``clan_chat_name`` config matches the payload's in-game clan name and whose
``clan_broadcast_tracking`` flag is on. Stronger than a shared secret: forging
requires an authed plugin account that is a member of the group, and the blast
radius is capped to groups that explicitly opted in.

This intake ALSO owns the broadcast half of the two-way chat bridge. ``CLAN_MESSAGE``
lines reach the server only as ``clan_broadcast`` payloads, but in game they sit
in the same chat box as player speech, so ``_mirror_to_bridge`` stages the raw
line for the group's bridge channel (``services/clan_chat_bridge``) right after
relayer auth — ahead of the parse, the tracked-kind filter, the tracking group
binding and any record or notification, none of which the mirror depends on.
Bridge and tracking are independent opt-ins; a group may run either alone.

Dedupe is three-layered, because N clanmates may relay the same line and
queue redelivery must be a no-op:
1. Redis ``SET NX EX`` on (clan, message) — collapses concurrent relayers
   (fail-open, same idiom as raid_dedupe).
2. A DETERMINISTIC guid ``cc:sha256(clan|message|10-min bucket)`` — every
   relayer computes the same one, so ``ensure_can_create`` makes late replays
   idempotent through the unbounded GUID check (the 2026-08-02 lesson). The
   relayer's own payload guid is useless here: N relayers, N guids.
3. The pet/notification paths ride their existing unique-constraint guards.
"""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta

# NOTE: utils.format (name normalization) is imported lazily inside the
# functions that need it — at module scope it pulls in db/ops and closes an
# import cycle when this package loads before ``db`` does.
from utils.clan_broadcasts import (
    TRACKED_KINDS,
    clan_slug,
    clean_broadcast_text,
    parse_broadcast,
)

from .common import (
    DatabaseOperations,
    RedisClient,
    SubmissionResponse,
    _is_temp_account_hash,
    create_notification,
    debug_print,
    ensure_can_create,
    ensure_item_by_name,
    ensure_player_and_auth,
    get_true_item_value,
    received_at,
    redis_updates,
    screenshot_required,
    select_session_and_flag,
)
from .raid_dedupe import _bundle_is_new

redis_client = RedisClient()
db = DatabaseOperations()

#: GroupConfiguration keys (registry: web_api/config_registry.py).
TRACKING_KEY = "clan_broadcast_tracking"
MIN_VALUE_KEY = "clan_broadcast_min_value"
CLAN_NAME_KEY = "clan_chat_name"
#: Escape hatch from the images-only gate. ``only_send_messages_with_images``
#: is aimed at plugin submissions, which CAN carry a screenshot; a relayed
#: broadcast never can, so that setting alone left a group recording chat rows
#: and notifying nothing — the tracking feature silently half-dead. Defaults ON
#: (opting into tracking is opting into imageless notifications from it); a
#: group that would rather have silence than an imageless embed turns it off.
NOTIFY_WITHOUT_IMAGES_KEY = "clan_broadcast_notify_without_images"

#: Fallback npc_list row for chat drops whose line names no resolvable source
#: (untradeables, wordings without a "from X" clause, novel/garbled names).
#: Chat text must never mint an npc_list row, and inventing one per item would
#: pollute real drop-source leaderboards.
SENTINEL_NPC_NAME = "Clan Broadcast"

#: Seconds two relays of the same line are collapsed for. Longer than
#: TrackScape's 10s equivalent because our queue-mode consumer can run behind;
#: bounded so a genuinely repeated broadcast (same player, same item, later
#: kill printing an identical line) is never eaten — those are minutes apart.
RELAY_SEEN_TTL_SECONDS = 60

#: Deterministic-guid time bucket. Two relayers straddling a bucket edge can
#: only double-write if Redis (layer 1) is ALSO down; accepted.
GUID_BUCKET_SECONDS = 600

#: Per-relayer ceiling. A real clan channel broadcasts nowhere near this;
#: sustained bursts above it are a spoofing/loop signal, not gameplay.
RELAYER_RATE_LIMIT_PER_MIN = 30

#: Plugin-user reconciliation. A broadcast about a plugin user is held this
#: long before the chat copy is recorded, giving their own (richer) submission
#: time to arrive — it usually lags the broadcast only by image upload + queue
#: latency, but backlogs run minutes, so the grace is generous. The look-back
#: is wider than the grace so a plugin copy that BEAT the broadcast is found
#: too.
PLUGIN_GRACE_SECONDS = 300
PLUGIN_COPY_LOOKBACK_SECONDS = 600
DEFERRED_ZSET_KEY = "clanchat:deferred"
DEFERRED_BATCH = 25
DEFERRED_MAX_ATTEMPTS = 3

_sentinel_npc_id = None


def _stat(field: str, count: int = 1) -> None:
    """Best-effort daily ops counters (``clanchat:stats:<YYYYMMDD>``)."""
    try:
        key = f"clanchat:stats:{datetime.now().strftime('%Y%m%d')}"
        client = redis_client.client
        client.hincrby(key, field, count)
        client.expire(key, 30 * 24 * 3600)
    except Exception:
        pass


def _relayer_within_rate_limit(relayer_player_id: int) -> bool:
    """~30 broadcasts/min per relayer, fail-open on Redis trouble."""
    try:
        minute = int(datetime.now().timestamp() // 60)
        key = f"clanchat:rate:{relayer_player_id}:{minute}"
        client = redis_client.client
        count = client.incr(key)
        if count == 1:
            client.expire(key, 120)
        return int(count) <= RELAYER_RATE_LIMIT_PER_MIN
    except Exception:
        return True


def _log_unknown_broadcast(text: str) -> None:
    """Sample-log unknown broadcast shapes (once per shape per hour).

    The parser only ever sees CLAN_MESSAGE system lines — Jagex-generated, no
    player chatter — so logging the line itself is privacy-safe and is exactly
    what we need to spot a Jagex rewording before it silently zeroes a kind.
    """
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if redis_client.client.set(f"clanchat:unknown:{digest}", "1", nx=True, ex=3600):
            print(f"[ClanBroadcast] Unknown broadcast shape: {text!r}")
    except Exception:
        pass


def _clan_slug(clan_name: str) -> str:
    return clan_slug(clan_name)


def _deterministic_guid(clan_slug: str, message: str, stamp: datetime) -> str:
    bucket = int(stamp.timestamp() // GUID_BUCKET_SECONDS)
    digest = hashlib.sha256(f"{clan_slug}|{message}|{bucket}".encode("utf-8")).hexdigest()
    return f"cc:{digest}"


def _bound_group_ids(session, relayer_player, clan_slug):
    """Group ids of the relayer bound to this clan and opted in, with their
    configured min-value floors: ``{group_id: min_value_int}``."""
    from sqlalchemy import text as sql_text

    rows = session.execute(
        sql_text("SELECT DISTINCT group_id FROM user_group_association WHERE player_id = :pid"),
        {"pid": relayer_player.player_id},
    ).all()
    candidate_ids = [gid for (gid,) in rows if gid and gid > 2]
    if not candidate_ids:
        return {}

    from utils import group_config as gc

    values = gc.get_bulk(session, candidate_ids, [TRACKING_KEY, MIN_VALUE_KEY, CLAN_NAME_KEY])
    bound = {}
    for gid in candidate_ids:
        if not gc.is_truthy(values.get((gid, TRACKING_KEY))):
            continue
        configured = _clan_slug(values.get((gid, CLAN_NAME_KEY)) or "")
        if not configured or configured != clan_slug:
            continue
        try:
            floor = int(values.get((gid, MIN_VALUE_KEY)) or 0)
        except (TypeError, ValueError):
            floor = 0
        bound[gid] = floor
    return bound


def _find_subject_in_groups(session, subject_name: str, group_ids):
    """Resolve the broadcast subject against the bound groups' rosters ONLY.

    Exact name first (indexed), then a normalized scan of the bound rosters —
    broadcast names arrive with the same display quirks WOM sync stores
    (spacing/underscores), and clans are a few hundred rows at most. Returns
    (player, member_group_ids) or (None, []).
    """
    from sqlalchemy import text as sql_text

    from db.models import Player

    if not group_ids:
        return None, []

    from utils.format import normalize_player_display_equivalence

    def _membership(player_id):
        rows = session.execute(
            sql_text(
                "SELECT DISTINCT group_id FROM user_group_association "
                "WHERE player_id = :pid AND group_id IN :gids"
            ),
            {"pid": player_id, "gids": tuple(group_ids)},
        ).all()
        return [gid for (gid,) in rows]

    exact = session.query(Player).filter(Player.player_name == subject_name).first()
    if exact:
        member_of = _membership(exact.player_id)
        if member_of:
            return exact, member_of

    wanted = normalize_player_display_equivalence(subject_name)
    roster = session.execute(
        sql_text(
            "SELECT DISTINCT p.player_id, p.player_name FROM players p "
            "JOIN user_group_association uga ON uga.player_id = p.player_id "
            "WHERE uga.group_id IN :gids"
        ),
        {"gids": tuple(group_ids)},
    ).all()
    for player_id, player_name in roster:
        if normalize_player_display_equivalence(player_name or "") == wanted:
            player = session.query(Player).filter(Player.player_id == player_id).first()
            if player:
                return player, _membership(player_id)
    return None, []


async def _ensure_sentinel_npc_id(session):
    """Get-or-create the sentinel npc_list row (module-cached)."""
    global _sentinel_npc_id
    if _sentinel_npc_id is not None:
        return _sentinel_npc_id
    from db.models import NpcList

    row = session.query(NpcList).filter(NpcList.npc_name == SENTINEL_NPC_NAME).first()
    if row is None:
        try:
            row = NpcList(npc_name=SENTINEL_NPC_NAME)
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            row = session.query(NpcList).filter(NpcList.npc_name == SENTINEL_NPC_NAME).first()
            if row is None:
                return None
    _sentinel_npc_id = row.npc_id
    return _sentinel_npc_id


def _record_group_exclusions(session, drop_id, exclusions: dict) -> None:
    """Durable ``drop_group_moderation`` 'excluded' rows (``{gid: reason}``).

    Same mechanism as manual_submission_policy withholds: write-time Redis
    filtering alone would leak the drop back onto a non-opted group's board on
    the next rebuild.
    """
    if not exclusions or not drop_id:
        return
    from db.models import DropGroupModeration

    for gid in sorted(exclusions):
        session.add(
            DropGroupModeration(
                drop_id=drop_id,
                group_id=gid,
                status="excluded",
                reason=exclusions[gid][:64],
            )
        )


def _pb_time_to_ms(time_text) -> int:
    """Broadcast display time ("21:55.80", "1:04") → milliseconds, 0 on junk."""
    from utils.format import convert_to_ms

    try:
        ms = convert_to_ms(str(time_text or "").strip().rstrip("."))
        return max(int(ms), 0) if ms else 0
    except Exception:
        return 0


def _resolve_pb_npc(session, activity):
    """PB activity text → ``(npc_id, canonical_name)``; see :func:`_resolve_npc`."""
    return _resolve_npc(session, activity)


def _resolve_npc(session, name_text):
    """Broadcast NPC/activity text → ``(npc_id, canonical_name)`` or ``(None, None)``.

    Resolve-only, deliberately: unlike ``ensure_npc_id_for_player`` this NEVER
    mints an npc_list row — a garbled or novel name from chat skips (PBs) or
    falls back to the sentinel (drops) rather than polluting the boss list.
    Same canonicalization + variant-slug matching the plugin path uses, so both
    paths land on one row.
    """
    from sqlalchemy import bindparam
    from sqlalchemy import text as sql_text

    from db.models import NpcList
    from utils.npc_names import (
        canonical_encounter_name,
        npc_match_variants,
        npc_primary_rank_sql_expr,
        npc_primary_variants,
        npc_slug_sql_expr,
    )

    name = canonical_encounter_name(clean_broadcast_text(name_text))
    if not name:
        return None, None
    row = (
        session.query(NpcList.npc_id, NpcList.npc_name)
        .filter(NpcList.npc_name == name)
        .first()
    )
    if row:
        return int(row[0]), row[1]
    variants = npc_match_variants(name)
    if not variants:
        return None, None
    norm = session.execute(
        sql_text(
            f"SELECT n.npc_id, n.npc_name FROM npc_list n "
            f"WHERE {npc_slug_sql_expr('n.npc_name')} IN :variants "
            f"ORDER BY {npc_primary_rank_sql_expr('n.npc_name')} ASC, n.npc_id ASC "
            f"LIMIT 1"
        ).bindparams(
            bindparam("variants", expanding=True),
            bindparam("primary_variants", expanding=True),
        ),
        {"variants": variants, "primary_variants": npc_primary_variants(name)},
    ).first()
    if norm:
        return int(norm[0]), norm[1]
    return None, None


def _pb_bracket(parsed) -> str:
    """Canonical team-size label for a PB broadcast (raids carry it in the
    line; everything else brackets Solo, same default the plugin path uses)."""
    from utils.npc_names import sanitize_team_size

    return sanitize_team_size(parsed.extra.get("team_size") or "Solo")


async def _plugin_copy_exists(session, subject, parsed, stamp) -> bool:
    """Did the subject's own plugin (or a manual submit) already record this?

    Matching is deliberately loose — same player, same item, close in time —
    because a broadcast line carries no guid, npc or kc to match harder on.
    Pets and clog slots are once-per-item, so bare existence answers those.
    A PB is covered when the stored (player, boss, bracket) time is already
    equal or faster. Anything that isn't a ``clan_chat`` row counts as
    coverage.
    """
    if parsed.kind == "personal_best":
        ms = _pb_time_to_ms(parsed.extra.get("time_text"))
        npc_id, _name = _resolve_pb_npc(session, parsed.extra.get("activity"))
        if not npc_id or ms <= 0:
            # Unresolvable either way — the record path will skip it too.
            return False
        from db.models import PersonalBestEntry

        return (
            session.query(PersonalBestEntry.id)
            .filter(
                PersonalBestEntry.player_id == subject.player_id,
                PersonalBestEntry.npc_id == npc_id,
                PersonalBestEntry.team_size == _pb_bracket(parsed),
                PersonalBestEntry.personal_best > 0,
                PersonalBestEntry.personal_best <= ms,
            )
            .first()
        ) is not None

    item = await ensure_item_by_name(session, parsed.item_name)
    if not item:
        return False

    if parsed.kind in ("item_drop", "raid_drop", "clue_item"):
        from sqlalchemy import or_

        from db.models import Drop

        window_start = stamp - timedelta(seconds=PLUGIN_COPY_LOOKBACK_SECONDS)
        return (
            session.query(Drop.drop_id)
            .filter(
                Drop.player_id == subject.player_id,
                Drop.item_id == item.item_id,
                Drop.date_added >= window_start,
                or_(Drop.source.is_(None), Drop.source != "clan_chat"),
            )
            .first()
        ) is not None

    if parsed.kind == "pet":
        from db.models import PlayerPet

        return (
            session.query(PlayerPet.player_id)
            .filter(
                PlayerPet.player_id == subject.player_id,
                PlayerPet.item_id == item.item_id,
            )
            .first()
        ) is not None

    if parsed.kind == "collection_log":
        from db.models import CollectionLogEntry

        return (
            session.query(CollectionLogEntry.log_id)
            .filter(
                CollectionLogEntry.player_id == subject.player_id,
                CollectionLogEntry.item_id == item.item_id,
            )
            .first()
        ) is not None

    return False


def _defer_broadcast(broadcast_data, world_type) -> bool:
    """Park a plugin-user broadcast for the grace window (see module docstring).

    The ORIGINAL payload is stored, so the replay recomputes everything —
    including ``received_at`` (the payload's ``_received_at`` survives the
    round-trip) and the deterministic guid — exactly as the first pass did.
    """
    try:
        entry = json.dumps(
            {"broadcast_data": broadcast_data, "world_type": world_type, "attempts": 0}
        )
        redis_client.client.zadd(
            DEFERRED_ZSET_KEY, {entry: time.time() + PLUGIN_GRACE_SECONDS}
        )
        return True
    except Exception as e:
        print(f"[ClanBroadcast] defer failed: {e}")
        return False


async def process_due_deferred_broadcasts(limit: int = DEFERRED_BATCH) -> int:
    """Replay deferred plugin-user broadcasts whose grace window has expired.

    Called by the webhook consumer's maintenance tick (~1/min). Each entry is
    claimed with ZREM before processing so a concurrent sweeper can't double-
    run it; a failed replay gets one delayed retry, then is dropped — the
    deterministic-guid layer makes a lost entry safe to re-relay. Returns the
    number of entries replayed.
    """
    try:
        client = redis_client.client
        due = client.zrangebyscore(
            DEFERRED_ZSET_KEY, "-inf", time.time(), start=0, num=int(limit)
        )
    except Exception:
        return 0
    replayed = 0
    for raw in due or []:
        try:
            if not client.zrem(DEFERRED_ZSET_KEY, raw):
                continue
        except Exception:
            continue
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            entry = json.loads(text)
            await clan_broadcast_processor(
                entry.get("broadcast_data") or {},
                world_type=entry.get("world_type", "main"),
                _deferred_replay=True,
            )
            replayed += 1
        except Exception as e:
            print(f"[ClanBroadcast] deferred replay failed: {e}")
            try:
                entry = json.loads(text)
                attempts = int(entry.get("attempts") or 0) + 1
                if attempts < DEFERRED_MAX_ATTEMPTS:
                    entry["attempts"] = attempts
                    client.zadd(DEFERRED_ZSET_KEY, {json.dumps(entry): time.time() + 60})
            except Exception:
                pass
    return replayed


def _mirror_to_bridge(session, relayer, clan_slug: str, message: str, deferred_replay: bool) -> int:
    """Mirror the raw broadcast line into the clan's two-way bridge channels.

    Independent of everything the tracking half decides: the bridge channel is
    a SYNCED view of the in-game clan chat box, which shows broadcasts next to
    player speech, so the line goes out whether or not it parses, is a tracked
    kind, belongs to an opted-in group or produces a record and a notification.
    The two features are separate opt-ins (``clan_chat_bridge_enabled`` +
    channel vs ``clan_broadcast_tracking``) and either can be on alone.

    Deferred replays (the plugin-user grace window) skip it — their first pass
    already mirrored the line, minutes earlier. Returns channels staged.
    """
    if deferred_replay:
        return 0
    try:
        from services.clan_chat_bridge import mirror_broadcast_line, relayer_within_rate_limit

        if not relayer_within_rate_limit(relayer.player_id):
            _stat("bridge_rate_limited")
            return 0
        mirrored = mirror_broadcast_line(session, relayer.player_id, clan_slug, message)
        if mirrored:
            _stat("bridge_mirrored")
        return mirrored
    except Exception as e:
        # Mirroring is display; never let it cost the tracking record.
        print(f"[ClanBroadcast] bridge mirror failed: {e}")
        return 0


async def _images_gate_blocks(session, group_id) -> bool:
    """Whether this group's images-only setting suppresses a chat notification.

    Only bites when the group ALSO turned :data:`NOTIFY_WITHOUT_IMAGES_KEY`
    off — see that constant. Governs every broadcast kind so drops, PBs, pets
    and clog slots behave the same way; before the key existed drops and PBs
    were gated while pets and clogs notified regardless.
    """
    if not await screenshot_required(session, group_id):
        return False
    from utils import group_config as gc

    if gc.is_truthy(gc.get(session, group_id, NOTIFY_WITHOUT_IMAGES_KEY, "1")):
        return False
    _stat("notify_blocked_images")
    return True


def _notification_gate(group_id, group_config_values, unit_value, total_value):
    """Mirror drop_processor's per-group announce criteria."""
    from utils import group_config as gc

    min_raw = group_config_values.get((group_id, "minimum_value_to_notify"))
    try:
        min_value = int(min_raw) if min_raw is not None else 2_500_000
    except (TypeError, ValueError):
        min_value = 2_500_000
    send_stacks = gc.is_truthy(group_config_values.get((group_id, "send_stacks_of_items")))
    return int(unit_value) >= min_value or (send_stacks and int(total_value) > min_value)


async def clan_broadcast_processor(
    broadcast_data, external_session=None, world_type="main", _deferred_replay=False
):
    """Process one relayed clan broadcast line. See module docstring.

    ``_deferred_replay=True`` marks the second pass of the plugin-user
    reconciliation (process_due_deferred_broadcasts): the multi-relayer
    dedupe, the rate limit and the bridge mirror were already paid on the
    first pass and are skipped, and the plugin-user branch records instead of
    deferring again.
    """

    session, use_external_session = select_session_and_flag(external_session)

    if world_type != "main":
        # League/seasonal clans broadcast too, but chat tracking is main-world
        # only in v1 (mirrors /manual-submit).
        return SubmissionResponse(True, "Clan broadcasts are not tracked for this world type")

    message_raw = broadcast_data.get("message")
    clan_name = broadcast_data.get("clan_name")
    relayer_name = broadcast_data.get("player_name", broadcast_data.get("player"))
    account_hash = broadcast_data.get("acc_hash")
    auth_key = broadcast_data.get("auth_key", None)
    if not message_raw or not clan_name or not relayer_name or not account_hash:
        return SubmissionResponse(False, "Missing clan broadcast fields")

    _stat("relayed")
    message = clean_broadcast_text(message_raw)

    # Auth runs BEFORE parsing (it used to follow it) because the chat-bridge
    # mirror below depends on it: only an authenticated clanmate's line may
    # reach a Discord channel.
    relayer, authed, user_exists = await ensure_player_and_auth(
        session, str(relayer_name).strip(), str(account_hash), auth_key
    )
    if not relayer or not user_exists or not authed:
        _stat("relayer_auth_failed")
        return SubmissionResponse(False, "Relayer failed auth check")

    clan_slug = _clan_slug(clan_name)
    if not clan_slug:
        return SubmissionResponse(False, "Missing clan name")

    mirrored = _mirror_to_bridge(session, relayer, clan_slug, message, _deferred_replay)
    mirror_note = f" (mirrored to {mirrored} bridge channel(s))" if mirrored else ""

    parsed = parse_broadcast(message)
    if parsed is None:
        _stat("unparsed")
        _log_unknown_broadcast(message)
        return SubmissionResponse(mirrored > 0, f"Unrecognized clan broadcast{mirror_note}")
    _stat(f"kind:{parsed.kind}")
    if parsed.kind not in TRACKED_KINDS or not parsed.player:
        # Recognized-but-untracked (quests, PKs, roster churn, ...): a clean
        # no-op, not a rejection — the relayer did nothing wrong, and the line
        # still belongs in the bridge channel.
        return SubmissionResponse(
            True, f"Broadcast kind '{parsed.kind}' is not tracked{mirror_note}"
        )

    if not _deferred_replay and not _relayer_within_rate_limit(relayer.player_id):
        _stat("rate_limited")
        return SubmissionResponse(False, f"Clan broadcast rate limit exceeded{mirror_note}")

    bound_groups = _bound_group_ids(session, relayer, clan_slug)
    if not bound_groups:
        _stat("no_bound_group")
        return SubmissionResponse(
            mirrored > 0,
            "No group of yours has clan broadcast tracking enabled for this clan "
            f"(check the group's clan_chat_name setting){mirror_note}",
        )

    # Layer-1 dedupe: collapse the same line arriving from several relayers.
    # Skipped on deferred replay — the first pass already claimed the line
    # (and marked this key) before parking it.
    if not _deferred_replay:
        seen_key = f"clanchat:seen:{clan_slug}:{hashlib.sha256(message.encode('utf-8')).hexdigest()[:24]}"
        if not _bundle_is_new(seen_key, RELAY_SEEN_TTL_SECONDS):
            _stat("duplicate_relay")
            return SubmissionResponse(True, "Broadcast already relayed by another clanmate")

    subject, subject_group_ids = _find_subject_in_groups(
        session, str(parsed.player).strip(), list(bound_groups)
    )
    if subject is None:
        _stat("subject_not_in_roster")
        return SubmissionResponse(
            False,
            f"'{parsed.player}' is not on the roster of a bound group — "
            "chat tracking never creates players; run a WOM member sync",
        )
    stamp = received_at(broadcast_data)

    if subject.account_hash and not _is_temp_account_hash(subject.account_hash):
        # Plugin-user reconciliation. Their own structured submission is
        # strictly richer, so it wins whenever it exists — but the plugin can
        # miss a drop (client closed, plugin off, mobile), so the chat copy is
        # a fallback, decided AFTER the grace window instead of never.
        if await _plugin_copy_exists(session, subject, parsed, stamp):
            _stat("suppressed_plugin_covered")
            return SubmissionResponse(
                True, f"{subject.player_name}'s own submission covers this; chat copy ignored"
            )
        if not _deferred_replay:
            if _defer_broadcast(broadcast_data, world_type):
                _stat("deferred_plugin_user")
                return SubmissionResponse(
                    True,
                    f"{subject.player_name} uses the plugin; chat copy deferred "
                    f"{PLUGIN_GRACE_SECONDS}s pending their own submission",
                )
            # Redis defer unavailable: keep the conservative pre-reconciliation
            # behaviour — suppressing risks a missed record, recording risks a
            # double-count, and only one of those is self-healing.
            _stat("suppressed_plugin_user")
            return SubmissionResponse(
                True, f"{subject.player_name} uses the plugin; chat copy ignored"
            )
        # Grace expired and still no plugin copy: the plugin missed it —
        # record the chat fallback below.
        _stat("recovered_plugin_gap")

    cc_guid = _deterministic_guid(clan_slug, message, stamp)
    await asyncio.sleep(0)

    if parsed.kind in ("item_drop", "raid_drop", "clue_item"):
        return await _process_chat_drop(
            session, use_external_session, broadcast_data, parsed, subject,
            subject_group_ids, bound_groups, cc_guid, stamp, clan_name,
        )
    if parsed.kind == "pet":
        return await _process_chat_pet(
            session, use_external_session, parsed, subject, subject_group_ids,
            bound_groups, cc_guid, stamp, message,
        )
    if parsed.kind == "collection_log":
        return await _process_chat_clog(
            session, use_external_session, parsed, subject, subject_group_ids,
            bound_groups, cc_guid,
        )
    if parsed.kind == "personal_best":
        return await _process_chat_pb(
            session, use_external_session, parsed, subject, subject_group_ids,
            cc_guid, stamp,
        )
    return SubmissionResponse(True, f"Broadcast kind '{parsed.kind}' is not tracked")


async def _process_chat_drop(
    session, use_external_session, broadcast_data, parsed, subject,
    subject_group_ids, bound_groups, cc_guid, stamp, clan_name,
):
    if not await ensure_can_create(session, cc_guid, "clan_broadcast"):
        _stat("duplicate_guid")
        return SubmissionResponse(True, "Broadcast already recorded")

    item = await ensure_item_by_name(session, parsed.item_name)
    if not item:
        _stat("unknown_item")
        return SubmissionResponse(False, f"Item {parsed.item_name} could not be resolved")

    # Server pricing is authoritative; the broadcast's coin figure (stack
    # total) only sanity-flags gross divergence. Raid/untradeable lines carry
    # no figure at all — provided_value=0 lets the GE price stand alone.
    text_total = parsed.value_gp or 0
    text_unit = int(text_total / parsed.quantity) if text_total and parsed.quantity else 0
    unit_value = int(await get_true_item_value(parsed.item_name, text_unit, item_id=item.item_id))
    total_value = unit_value * parsed.quantity
    if text_total and total_value and (
        total_value > text_total * 2 or text_total > total_value * 2
    ):
        print(
            f"[ClanBroadcast] Value divergence for {parsed.item_name}: "
            f"broadcast said {text_total}, priced {total_value}; using priced"
        )
        _stat("value_divergence")

    # Storage floor: only groups whose clan_broadcast_min_value the drop meets
    # count it; if none do, no row is written at all.
    qualifying = {
        gid for gid in subject_group_ids if total_value >= bound_groups.get(gid, 0)
    }
    if not qualifying:
        _stat("below_min_value")
        return SubmissionResponse(True, "Drop below every bound group's clan broadcast minimum")

    # Attribute to the source the line named ("... from Alchemical Hydra") when
    # it resolves to a real npc_list row; the sentinel covers the rest —
    # untradeables and wordings that print no source, and novel/garbled names,
    # which must never mint a boss row from chat text.
    npc_id, npc_name = _resolve_npc(session, parsed.extra.get("source"))
    if npc_id is None:
        npc_id = await _ensure_sentinel_npc_id(session)
        npc_name = SENTINEL_NPC_NAME
        _stat("drop_npc_sentinel")
        if npc_id is None:
            return SubmissionResponse(False, "Sentinel NPC row unavailable")
    else:
        _stat("drop_npc_resolved")

    drop = await db.create_drop_object(
        item_id=item.item_id,
        player_id=subject.player_id,
        date_received=stamp,
        npc_id=npc_id,
        value=unit_value,
        quantity=parsed.quantity,
        image_url=None,
        authed=False,
        attachment_url=None,
        attachment_type=None,
        used_api=False,
        unique_id=cc_guid,
        existing_session=session if use_external_session else None,
        source="clan_chat",
        kill_count=None,
    )
    if not drop:
        return SubmissionResponse(False, "Failed to create chat drop")
    _stat("drops_recorded")

    # Groups that must NOT count this drop: every real group of the subject
    # that isn't bound+opted-in for this clan, plus bound groups under floor.
    from sqlalchemy import text as sql_text

    subject_all_gids = [
        gid for (gid,) in session.execute(
            sql_text("SELECT DISTINCT group_id FROM user_group_association WHERE player_id = :pid"),
            {"pid": subject.player_id},
        ).all() if gid and gid > 2
    ]
    exclusions = {}
    for gid in subject_all_gids:
        if gid not in qualifying:
            reason = (
                "clan_broadcast:below_min" if gid in bound_groups and gid in subject_group_ids
                else "clan_broadcast:not_opted_in"
            )
            exclusions[gid] = reason
    try:
        _record_group_exclusions(session, drop.drop_id, exclusions)
        if use_external_session:
            session.flush()
        else:
            session.commit()
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        print(f"[ClanBroadcast] Failed to record group exclusions for drop {drop.drop_id}: {e}")

    try:
        redis_updates.add_to_player(
            subject, drop, world_type="main",
            item_name=parsed.item_name, npc_name=npc_name,
            exclude_group_ids=set(exclusions),
        )
    except Exception as e:
        print(f"[ClanBroadcast] Redis update failed for drop {drop.drop_id}: {e}")

    # Notifications for qualifying groups, mirroring drop_processor's gates.
    # No points, no events, no splits — deliberate (see module docstring).
    from utils import group_config as gc

    group_config_values = gc.get_bulk(
        session, sorted(qualifying), ["minimum_value_to_notify", "send_stacks_of_items"]
    )
    notified = 0
    for gid in sorted(qualifying):
        if not _notification_gate(gid, group_config_values, unit_value, total_value):
            continue
        if await _images_gate_blocks(session, gid):
            continue
        notification_data = {
            "drop_id": drop.drop_id,
            "guid": cc_guid,
            "item_name": parsed.item_name,
            "npc_name": npc_name,
            "value": unit_value,
            "quantity": parsed.quantity,
            "total_value": total_value,
            "kill_count": None,
            "player_name": subject.player_name,
            "player_id": subject.player_id,
            "image_url": None,
            "video_key": None,
            "attachment_type": None,
            "points_awarded": 0,
            "has_awarded_points": False,
            "group_points_awarded": 0,
            "group_points_receiver_total": 0,
            "group_points_member_count": 0,
            "group_points_members_awarded": [],
            "world_type": "main",
            "plugin_version": broadcast_data.get("p_v"),
            "via_clan_broadcast": True,
            "clan_name": clean_broadcast_text(clan_name),
        }
        await create_notification(
            "drop", subject.player_id, notification_data, gid,
            existing_session=session if use_external_session else None,
        )
        notified += 1

    debug_print(
        f"[ClanBroadcast] Recorded {parsed.item_name} x{parsed.quantity} "
        f"({total_value} gp) from {npc_name} for {subject.player_name} "
        f"via {clean_broadcast_text(clan_name)}; "
        f"groups={sorted(qualifying)} notified={notified}"
    )
    return SubmissionResponse(True, f"Recorded chat drop for {subject.player_name}")


async def _process_chat_pb(
    session, use_external_session, parsed, subject, subject_group_ids, cc_guid, stamp,
):
    """Record a broadcast personal best, mirroring pb_processor's write gate.

    The invariant that matters (ticket #361 territory): a chat time may only
    CREATE a bracket row or IMPROVE it — a stored faster time is never
    overwritten. Bracket comes from the broadcast itself for raids
    ("(Team Size: N)"); everything else is Solo, the same default the plugin
    path applies. Unknown activities skip rather than minting NPCs. No group
    points, no DMs — chat rows stay second-class (module docstring).
    """
    if not await ensure_can_create(session, cc_guid, "pb"):
        _stat("duplicate_guid")
        return SubmissionResponse(True, "PB broadcast already recorded")

    time_ms = _pb_time_to_ms(parsed.extra.get("time_text"))
    if time_ms <= 0:
        _stat("pb_unparsed_time")
        return SubmissionResponse(True, "PB time not parseable; skipped")
    npc_id, boss_name = _resolve_pb_npc(session, parsed.extra.get("activity"))
    if not npc_id:
        _stat("pb_npc_unresolved")
        return SubmissionResponse(
            True, f"PB activity {parsed.extra.get('activity')!r} is not a known boss; skipped"
        )
    team_size = _pb_bracket(parsed)

    from db.models import PersonalBestEntry

    old_time = None
    row = (
        session.query(PersonalBestEntry)
        .filter(
            PersonalBestEntry.player_id == subject.player_id,
            PersonalBestEntry.npc_id == npc_id,
            PersonalBestEntry.team_size == team_size,
        )
        .first()
    )
    if row is not None:
        if row.personal_best and 0 < row.personal_best <= time_ms:
            _stat("pb_not_better")
            return SubmissionResponse(True, "Stored PB is already equal or faster")
        old_time = row.personal_best
        row.personal_best = time_ms
        row.kill_time = time_ms
        row.new_pb = True
        row.date_added = stamp
    else:
        row = PersonalBestEntry(
            player_id=subject.player_id,
            npc_id=npc_id,
            team_size=team_size,
            new_pb=True,
            personal_best=time_ms,
            kill_time=time_ms,
            date_added=stamp,
            image_url="",
            video_url=None,
            used_api=False,
            unique_id=cc_guid,
        )
        # SAVEPOINT like pb_processor: a unique_id replay must discard only
        # this row, never poison the caller's staged work.
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except Exception:
            debug_print("[ClanBroadcast] PB row create failed (likely replay)")
            return SubmissionResponse(True, "PB broadcast already recorded")
    if use_external_session:
        session.flush()
    else:
        session.commit()
    _stat("pbs_recorded")

    # Near-real-time HOF refresh + top-25 ticker, mirroring pb_processor.
    try:
        redis_client.client.rpush(
            "hof:refresh:queue",
            json.dumps({"player_id": int(subject.player_id), "npc_id": int(npc_id)}),
        )
    except Exception:
        pass
    try:
        from services.realtime import publish_feed_personal_best
        from utils.format import convert_from_ms
        from utils.pb_rank import pb_board_rank

        ranked = pb_board_rank(session, npc_id, team_size, subject.player_id)
        if ranked is not None and ranked[0] <= 25:
            publish_feed_personal_best(
                player_id=subject.player_id,
                player_name=subject.player_name,
                npc_id=npc_id,
                npc_name=boss_name,
                time_ms=time_ms,
                time_display=convert_from_ms(time_ms),
                team_size=team_size,
                rank=ranked[0],
            )
    except Exception as e:
        debug_print(f"[ClanBroadcast] Ticker PB publish failed: {e}")

    # Group notifications: same notify_pbs + screenshot gates as pb_processor.
    from utils import group_config as gc

    bulk = gc.get_bulk(session, sorted(set(subject_group_ids)), ["notify_pbs"])
    notified = 0
    for gid in sorted(set(subject_group_ids)):
        if not gc.is_truthy(bulk.get((gid, "notify_pbs"))):
            continue
        if await _images_gate_blocks(session, gid):
            continue
        notification_data = {
            "player_name": subject.player_name,
            "player_id": subject.player_id,
            "pb_id": row.id,
            "guid": cc_guid,
            "npc_id": npc_id,
            "boss_name": boss_name,
            "time_ms": time_ms,
            "old_time_ms": old_time,
            "team_size": team_size,
            "kill_time_ms": time_ms,
            "image_url": "",
            "video_key": None,
            "group_points_awarded": 0,
            "group_points_receiver_total": 0,
            "group_points_member_count": 0,
            "group_points_members_awarded": [],
            "world_type": "main",
            "plugin_version": None,
            "via_clan_broadcast": True,
        }
        await create_notification(
            "pb", subject.player_id, notification_data, gid,
            existing_session=session if use_external_session else None,
        )
        notified += 1

    return SubmissionResponse(
        True,
        f"Recorded chat PB for {subject.player_name}: {boss_name} ({team_size}) "
        f"in {time_ms}ms ({notified} groups notified)",
    )


async def _process_chat_pet(
    session, use_external_session, parsed, subject, subject_group_ids,
    bound_groups, cc_guid, stamp, message,
):
    if not await ensure_can_create(session, cc_guid, "pet"):
        _stat("duplicate_guid")
        return SubmissionResponse(True, "Pet broadcast already recorded")

    pet_item = await ensure_item_by_name(session, parsed.item_name)
    pet_item_id = pet_item.item_id if pet_item else None

    from db.models import PlayerPet

    is_new_pet = True
    if pet_item_id:
        existing = (
            session.query(PlayerPet)
            .filter(PlayerPet.player_id == subject.player_id, PlayerPet.item_id == pet_item_id)
            .first()
        )
        is_new_pet = existing is None

    if is_new_pet and pet_item_id:
        try:
            # SAVEPOINT like pet_processor: a unique_id replay must discard
            # only this row, never poison the caller's staged work.
            with session.begin_nested():
                session.add(PlayerPet(
                    player_id=subject.player_id,
                    item_id=pet_item_id,
                    pet_name=parsed.item_name,
                    unique_id=cc_guid,
                    date_added=stamp,
                ))
                session.flush()
            if use_external_session:
                session.flush()
            else:
                session.commit()
            _stat("pets_recorded")
        except Exception as e:
            debug_print(f"[ClanBroadcast] Pet row create failed (likely replay): {e}")
            is_new_pet = False

    milestone = None
    if parsed.extra.get("milestone_count") is not None:
        milestone = f"{parsed.extra['milestone_count']:,} {parsed.extra.get('milestone_unit') or ''}".strip()

    from utils.osrs_pets import skilling_pet_source

    source = skilling_pet_source(parsed.item_name)
    notified = 0
    for gid in sorted(set(subject_group_ids)):
        if await _images_gate_blocks(session, gid):
            continue
        notification_data = {
            "group_id": gid,
            "player_name": subject.player_name,
            "player_id": subject.player_id,
            "guid": cc_guid,
            "pet_name": parsed.item_name,
            "source": source,
            "npc_name": source,
            "killcount": None,
            "milestone": milestone,
            "duplicate": not is_new_pet,
            "previously_owned": None,
            "game_message": message,
            "image_url": "",
            "video_key": None,
            "item_id": pet_item_id,
            "npc_id": None,
            "is_new_pet": is_new_pet,
            "group_points_awarded": 0,
            "group_points_receiver_total": 0,
            "group_points_member_count": 0,
            "group_points_members_awarded": [],
            "world_type": "main",
            "plugin_version": None,
            "via_clan_broadcast": True,
        }
        await create_notification(
            "pet", subject.player_id, notification_data, gid,
            existing_session=session if use_external_session else None,
        )
        notified += 1
    return SubmissionResponse(True, f"Recorded chat pet for {subject.player_name} ({notified} groups)")


async def _process_chat_clog(
    session, use_external_session, parsed, subject, subject_group_ids,
    bound_groups, cc_guid,
):
    """Collection-log broadcasts are notification-only in v1: writing
    CollectionLogEntry rows from text would fight the plugin clog pipeline's
    slot-count reconciliation, and the subject's totals already come from WOM
    (``log_slots``)."""
    try:
        # No DB row means no GUID backstop — persist the dedupe in Redis for a
        # day so a queue replay hours later stays silent.
        if not redis_client.client.set(f"clanchat:clognotif:{cc_guid}", "1", nx=True, ex=86400):
            return SubmissionResponse(True, "Collection log broadcast already notified")
    except Exception:
        pass

    item = await ensure_item_by_name(session, parsed.item_name)
    _stat("clogs_notified")
    notified = 0
    for gid in sorted(set(subject_group_ids)):
        if await _images_gate_blocks(session, gid):
            continue
        notification_data = {
            "player_name": subject.player_name,
            "player_id": subject.player_id,
            "guid": cc_guid,
            "item_name": parsed.item_name,
            "npc_name": None,
            "image_url": "",
            "video_key": None,
            "kc_received": None,
            "item_id": item.item_id if item else None,
            "log_slots": parsed.extra.get("log_slots"),
            "group_points_awarded": 0,
            "group_points_receiver_total": 0,
            "group_points_member_count": 0,
            "group_points_members_awarded": [],
            "world_type": "main",
            "plugin_version": None,
            "via_clan_broadcast": True,
        }
        await create_notification(
            "clog", subject.player_id, notification_data, gid,
            existing_session=session if use_external_session else None,
        )
        notified += 1
    return SubmissionResponse(True, f"Notified clog broadcast for {subject.player_name} ({notified} groups)")
