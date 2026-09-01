"""Shared utilities and state for submissions processors.

This module centralizes common imports, shared caches, data classes,
and helper functions used by the various submission processors.

All functions/classes are exported with stable names to preserve
backward compatibility with the original `data.submissions` module.
"""

import asyncio
import contextlib
import contextvars
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from api.core import logger
from db import (
    models,
    CombatAchievementEntry,
    Drop,
    FeatureActivation,
    NotifiedSubmission,
    QuestCompletionEntry,
    PlayerDeath,
    DiaryCompletionEntry,
    PlayerPet,
    session,
    NpcList,
    IgnoredPlayer,
    Player,
    ItemList,
    PersonalBestEntry,
    CollectionLogEntry,
    User,
    Group,
    GroupConfiguration,
    UserConfiguration,
    NotificationQueue,
    SeasonalDrop,
    SeasonalPersonalBestEntry,
    SeasonalCollectionLogEntry,
    SeasonalCombatAchievementEntry,
    SeasonalPlayerPet,
    SeasonalQuestCompletionEntry,
)
from db.ops import DatabaseOperations, associate_player_ids, get_point_divisor
from sqlalchemy import func, text
from sqlalchemy.engine import Row

from services import redis_updates
from services.points import award_points_to_player

from utils.account_types import VALID_ACCOUNT_TYPES
from utils.ge_value import get_true_item_value
import osrs_api
from utils.wiseoldman import (
    check_user_by_id,
    check_user_by_username,
    check_group_by_id,
    fetch_group_members,
    get_collections_logged,
    get_player_boss_kills,
    get_player_metric,
)
from utils.redis import RedisClient
from utils.download import download_player_image, download_image
from utils.format import (
    convert_to_ms,
    format_number,
    get_command_id,
    get_extension_from_content_type,
    get_true_boss_name,
    replace_placeholders,
    convert_from_ms,
    normalize_player_display_equivalence,
)
import interactions
from utils.logger import LoggerClient
from db.app_logger import AppLogger


load_dotenv()
debug_level = "false"
debug = debug_level != "false"


def debug_print(message, **kwargs):
    if debug:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] DEBUG: {message}", **kwargs)


SEASONAL_WORLD_TYPE = "seasonal"


def apply_account_type(player, raw_value, world_type: str = "main"):
    """Persist the submitted ``account_type`` on the player row (last-write-wins).

    Invalid, absent, or non-main-world values are silently ignored — this must
    never affect submission processing. Seasonal (League) worlds are separate
    game accounts whose mode says nothing about the main account, so they never
    write here. The caller's session commit persists the change.
    """
    try:
        if player is None or raw_value is None or world_type != "main":
            return
        value = str(raw_value).strip().lower()
        if value in VALID_ACCOUNT_TYPES and player.account_type != value:
            player.account_type = value
    except Exception:
        pass


def envelope_from_plugin(submission_data: dict) -> bool:
    """Whether an event-engine envelope should read as plugin traffic
    (``used_api`` on the envelope — NOT the DB rows' transport flag).

    Plugin submissions reach us two ways: the direct plugin API and the
    Discord webhook reader (the plugin posts those embeds too). Both count
    as plugin for event submission policies; only manual website/command
    submissions (``intake_source == "manual"``) are non-plugin.
    """
    return submission_data.get("intake_source") != "manual"


# How far behind "now" an accepted-at stamp may be and still be believed.
# A submission that sat in the queue longer than this is more likely to be a
# replayed or hand-requeued entry than a live one, and dating a row hours into
# the past would silently rewrite a closed month's totals.
_RECEIVED_AT_MAX_LAG = timedelta(hours=6)


def received_at(submission_data: dict) -> datetime:
    """When the server ACCEPTED this submission, for stamping the row.

    Falls back to now() when the stamp is missing, unparseable, in the future,
    or implausibly old. The distinction matters at a month or day boundary:
    stamping at processing time means a queue backlog books kills earned before
    midnight into the next month's leaderboards, rollups and recaps — which is
    exactly what happened on 2026-08-01 when the queue ran ~107 minutes behind.

    Deliberately the SERVER's accept time (``enqueued_at``, set in
    api/routes/webhook.py), not the client's ``timestamp``: a player's clock is
    neither accurate nor trustworthy, and a spoofed one could rewrite history.
    """
    raw = (submission_data or {}).get("_received_at")
    if not raw:
        return datetime.now()
    try:
        stamped = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.now()
    if stamped.tzinfo is not None:
        stamped = stamped.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.now()
    if stamped > now or (now - stamped) > _RECEIVED_AT_MAX_LAG:
        return now
    return stamped


def get_config_prefix(world_type: str) -> str:
    """Return the GroupConfiguration key prefix for the given world type.

    Seasonal submissions use config keys prefixed with "seasonal_" so groups
    can define separate channel IDs, limits, and enabled flags for each mode.
    """
    return "seasonal_" if world_type == SEASONAL_WORLD_TYPE else ""


def get_seasonal_model(submission_type: str):
    """Return the seasonal ORM model class for the given submission type string."""
    return {
        "drop": SeasonalDrop,
        "personal_best": SeasonalPersonalBestEntry,
        "collection_log": SeasonalCollectionLogEntry,
        "combat_achievement": SeasonalCombatAchievementEntry,
        "pet": SeasonalPlayerPet,
        "quest": SeasonalQuestCompletionEntry,
    }.get(submission_type)


global_footer = os.getenv("DISCORD_MESSAGE_FOOTER")
redis_client = RedisClient()
db = DatabaseOperations()

last_channels_sent = []

app_logger = AppLogger()

# Cache WOM identity lookups to avoid repeated external API waits during bursts.
_wom_user_cache = {}
_WOM_USER_CACHE_TTL_SECONDS = 300

# Caches with size limits to prevent unbounded growth
MAX_CACHE_SIZE = 10000
npc_list = {}
player_list = {}

def _trim_cache(cache: dict, max_size: int = MAX_CACHE_SIZE) -> None:
    """Trim a cache dict to max_size by removing oldest entries (FIFO approximation)."""
    if len(cache) > max_size:
        # Remove roughly 20% of entries when we exceed the limit
        keys_to_remove = list(cache.keys())[: len(cache) - int(max_size * 0.8)]
        for key in keys_to_remove:
            cache.pop(key, None)

def cache_player(player_name: str, player_id: int) -> None:
    """Cache a player name to ID mapping with size limit."""
    _trim_cache(player_list)
    player_list[player_name] = player_id

def cache_npc(npc_name: str, npc_id: int) -> None:
    """Cache an NPC name to ID mapping with size limit."""
    _trim_cache(npc_list)
    npc_list[npc_name] = npc_id


async def get_wom_user_cached(player_name: str):
    now = time.time()
    cached = _wom_user_cache.get(player_name)
    if cached:
        value, ts = cached
        if now - ts < _WOM_USER_CACHE_TTL_SECONDS:
            return value
    try:
        value = await check_user_by_username(player_name)
    except Exception:
        value = (None, None, None, -1)
    _wom_user_cache[player_name] = (value, now)
    if len(_wom_user_cache) > 10000:
        # Trim oldest ~20% to bound memory.
        for key in list(_wom_user_cache.keys())[:2000]:
            _wom_user_cache.pop(key, None)
    return value


def _to_int_or_none(value):
    try:
        if value in (None, "", 0):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_total_level_from_wom_player(wom_player) -> int:
    if isinstance(wom_player, dict):
        return int(wom_player.get("total_level") or 0)
    try:
        overall = wom_player.latest_snapshot.data.skills.get("overall")
        return int(overall.level)
    except Exception:
        return 0


def _extract_ehb_from_wom_player(wom_player):
    """WOM efficient-hours-bossed from the identity shim (or a raw WOM player
    detail object); None when unknown — pre-upgrade cache entries lack the key,
    and None must never overwrite a stored value."""
    raw = (wom_player.get("ehb") if isinstance(wom_player, dict)
           else getattr(wom_player, "ehb", None))
    try:
        return round(float(raw), 2) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _apply_authoritative_wom_identity(
    db_session,
    player: Player | None,
    expected_wom_id: int | None,
    *,
    canonical_name: str | None = None,
    total_level: int | None = None,
    log_slots: int | None = None,
    account_hash: str | None = None,
    ehb: float | None = None,
):
    """
    Ensure the local player row reflects WOM's authoritative identity.

    Returns:
        tuple[player|None, bool]: (possibly replaced canonical player, changed_flag)
    """
    if player is None or expected_wom_id is None:
        return player, False

    changed = False
    current_wom_id = _to_int_or_none(getattr(player, "wom_id", None))
    if current_wom_id != expected_wom_id:
        canonical = (
            db_session.query(Player)
            .filter(Player.wom_id == expected_wom_id)
            .first()
        )
        if canonical and canonical.player_id != player.player_id:
            player = canonical
        else:
            player.wom_id = expected_wom_id
            changed = True

    if canonical_name and normalize_player_display_equivalence(player.player_name or "") != normalize_player_display_equivalence(canonical_name):
        player.player_name = canonical_name
        changed = True

    if total_level is not None and int(total_level) > 0 and int(player.total_level or 0) != int(total_level):
        player.total_level = int(total_level)
        changed = True

    if log_slots is not None and int(log_slots) >= 0 and player.log_slots != int(log_slots):
        player.log_slots = int(log_slots)
        changed = True

    if ehb is not None and float(ehb) >= 0 and (player.ehb is None or float(player.ehb) != float(ehb)):
        player.ehb = float(ehb)
        changed = True

    if account_hash:
        account_hash = str(account_hash)
        if not player.account_hash:
            existing_hash_owner = (
                db_session.query(Player)
                .filter(Player.account_hash == account_hash)
                .first()
            )
            if (
                existing_hash_owner
                and existing_hash_owner.player_id != player.player_id
                and _to_int_or_none(existing_hash_owner.wom_id) != expected_wom_id
            ):
                existing_hash_owner.account_hash = None
                changed = True
            player.account_hash = account_hash
            changed = True

    return player, changed


class SubmissionResponse:
    """Response object for API submission endpoints.

    Attributes:
        success (bool): Whether the submission was processed successfully.
        message (str): Descriptive message about the submission result.
        notice (str | None): Optional additional notice or warning.
    """

    def __init__(self, success, message, notice=None):
        self.success = success
        self.message = message
        self.notice = notice


class RawDropData:
    """Container class for raw drop submission data."""

    def __init__(self) -> None:
        pass


def _is_temp_account_hash(hash_value) -> bool:
    """True when hash_value is a temporary placeholder assigned during WOM group import."""
    return hash_value is not None and str(hash_value).startswith("wom_temp_")


def check_auth(
    player_name,
    account_hash,
    auth_key,
    external_session=None,
    resolved_player=None,
    resolved_by_name=False,
    expected_wom_id=None,
):
    """Authenticate a player against stored account hash.

    Returns:
        tuple[bool, bool]: (user_exists, authed)
    """

    use_external_session = external_session is not None
    if use_external_session:
        db_session = external_session
    else:
        db_session = session
    try:
        player = resolved_player
        if player is None:
            player = db_session.query(Player).filter(Player.player_name.ilike(player_name)).first()

        if not player:
            return False, False

        if player.account_hash:
            if str(account_hash) == str(player.account_hash):
                return True, True
            # A temporary WOM-import hash can be replaced by the player's real hash
            # the first time they authenticate via the RuneLite plugin.
            if _is_temp_account_hash(player.account_hash):
                conflicting = (
                    db_session.query(Player)
                    .filter(Player.account_hash == str(account_hash))
                    .first()
                    if account_hash
                    else None
                )
                if conflicting and conflicting.player_id != player.player_id:
                    return True, False
                player.account_hash = str(account_hash)
                try:
                    db_session.commit()
                    app_logger.log(
                        log_type="access",
                        data=f"Replaced temporary account hash for {player_name} (wom_id={player.wom_id})",
                        app_name="core",
                        description="check_auth",
                    )
                except Exception as e:
                    debug_print("Error replacing temp hash: " + str(e))
                    db_session.rollback()
                    return True, False
                return True, True
            return True, False
        else:
            # Never bind an account hash to a name-resolved row unless WOM identity was verified.
            if resolved_by_name:
                if expected_wom_id is None:
                    app_logger.log(
                        log_type="warning",
                        data=f"Rejected account hash bind for {player_name}: missing expected WOM identity",
                        app_name="core",
                        description="check_auth",
                    )
                    return True, False
                try:
                    expected_wom_id = int(expected_wom_id)
                except (TypeError, ValueError):
                    return True, False
                if int(player.wom_id or 0) != expected_wom_id:
                    app_logger.log(
                        log_type="warning",
                        data=(
                            f"Rejected account hash bind for {player_name}: "
                            f"row WOM {player.wom_id} != expected WOM {expected_wom_id}"
                        ),
                        app_name="core",
                        description="check_auth",
                    )
                    return True, False

            existing_player = (
                db_session.query(Player).filter(Player.account_hash == account_hash).first()
                if account_hash
                else None
            )
            if existing_player:
                if existing_player.player_id != player.player_id:
                    if expected_wom_id is not None:
                        try:
                            expected_wom_id = int(expected_wom_id)
                        except (TypeError, ValueError):
                            return True, False
                        if int(existing_player.wom_id or 0) == expected_wom_id:
                            return True, True
                    return True, False
                if (
                    normalize_player_display_equivalence(existing_player.player_name)
                    != normalize_player_display_equivalence(player_name)
                ):
                    existing_player.player_name = player_name
                    app_logger.log(
                        log_type="access",
                        data=f"Player {player_name} already exists with account hash {account_hash}, updating player name to {player_name}",
                        app_name="core",
                        description="check_auth",
                    )
                    try:
                        db_session.commit()
                    except Exception as e:
                        debug_print("Error committing player name change:" + str(e))
                        db_session.rollback()
            player.account_hash = str(account_hash)
            try:
                db_session.commit()
            except Exception as e:
                debug_print("Error committing player name change:" + str(e))
                db_session.rollback()
            return True, True
    except Exception as e:
        debug_print("Error checking auth:" + str(e))
        return False, False


def select_session_and_flag(external_session):
    """Return (session, use_external_session_flag).
    
    When no external session is provided, uses the global scoped session.
    The scoped session should be cleaned up via session.remove() after each request.
    """
    if external_session is not None:
        return external_session, True
    # Use the global scoped session - it will be cleaned up at request end
    return session, False


async def ensure_item_for_drop(session, item_id, item_name):
    """Ensure an item exists by id or name. Mirrors drop processor behavior.

    The RuneLite plugin always sends a numeric ``item_id`` straight from the
    game cache — it is authoritative. When that id isn't in ``items`` yet we
    mint the row directly from the (id, name) the plugin gave us. Previously
    this path fell through to a *name-based* wiki lookup, which returned None
    (and thus rejected the whole drop, never adding the item) for any new or
    renamed item whose wiki name didn't exactly match. Only when no usable id
    is present — manual website submissions carry a name only — do we fall
    back to the name -> ItemList wiki resolution.
    """

    try:
        iid = int(item_id) if item_id is not None else None
    except (TypeError, ValueError):
        iid = None

    if iid is not None and iid > 0:
        item = session.query(ItemList).filter(ItemList.item_id == iid).first()
        if item:
            # Self-heal a known item whose icon was never fetched.
            await _ensure_item_icon(iid)
            return item
        # New item id from the plugin: trust it and create the row directly.
        name = str(item_name).strip() if item_name else None
        try:
            session.rollback()
        except Exception:
            pass
        try:
            item = ItemList(item_id=iid, item_name=name, noted=0, stackable=0, stacked=0)
            session.add(item)
            session.commit()
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
            # A concurrent request may have inserted it; re-read before giving up.
            item = session.query(ItemList).filter(ItemList.item_id == iid).first()
            if not item:
                return None
        await _ensure_item_icon(iid)
        return item

    if item_name is not None:
        # No usable id (manual submissions): resolve the name via the wiki,
        # minting the row with the item's real game id.
        # Release any open transaction before awaiting external API calls.
        # Otherwise the Session can keep a pooled DB connection checked out
        # while waiting on network I/O.
        try:
            session.rollback()
        except Exception:
            pass
        try:
            return await ensure_item_by_name(session, item_name)
        except Exception:
            return None
    return None


async def _ensure_item_icon(item_id) -> None:
    """Best-effort: download the item's icon if it isn't cached yet.

    Never raises — a missing icon degrades to the placeholder on the website,
    it must not fail ingest.
    """
    try:
        from utils.item_images import ensure_item_image
        await ensure_item_image(item_id)
    except Exception:
        pass


async def screenshot_required(session, group_id) -> bool:
    """ Checks whether a group has configured that screenshots must be included for notifications to be created """
    from utils import group_config as gc
    value = gc.get(session, group_id, gc.ONLY_SEND_MESSAGES_WITH_IMAGES)
    return gc.is_truthy(value)

def check_group_point_system_active(group_id, external_session=None):
    """Whether the group's custom point system is enabled (subscription entitlement).

    Resolution + 60s caching live in db.entitlements; the external_session
    parameter is kept for call-site compatibility but no longer used.
    """
    from db.entitlements import group_has_entitlement
    return group_has_entitlement(group_id, "custom_points")
    

def _announce_new_player(session, player) -> None:
    """Site-wide ticker (rt:feed): sampled "new player started tracking" entry.

    New players arrive constantly, so the realtime gate (Redis SET NX EX)
    limits this to at most one ticker entry per cooldown window — the COUNT
    query only runs after winning that slot. Best-effort; never raises.
    """
    try:
        from sqlalchemy import func

        from services.realtime import feed_new_player_gate, publish_feed_new_player

        if not feed_new_player_gate():
            return
        total = session.query(func.count(Player.player_id)).scalar()
        publish_feed_new_player(player.player_id, player.player_name, total)
    except Exception as e:
        debug_print(f"Ticker new-player publish failed: {e}")


def _heal_stub_onto_hash_owner(session, player_by_wom, player_by_hash, expected_wom_id):
    """Resolve a WOM id parked on an import stub back to the account that owns the hash.

    Returns the row submissions should be attributed to.

    A ``wom_temp_`` row is a placeholder minted from a clan roster for someone who
    has never used the plugin. When one holds the WOM id for an RSN whose *real*,
    plugin-authed row already exists under the submitted account hash, the stub is
    not the player — it is a duplicate sitting in front of them.

    Without this, that arrangement is a permanent, silent outage for the account.
    ``ensure_player_and_auth`` resolves the RSN through WOM, lands on the stub, and
    ``check_auth`` then sees a temp hash whose replacement is blocked because
    another row already holds the submitted hash — so it returns
    ``(user_exists=True, authed=False)`` and every processor rejects the
    submission with "failed auth check". Nothing errors and nothing is queued;
    the drops are simply gone. That is what silently discarded ~3 weeks of one
    account's submissions from 2026-07-27 (player_id 7151 / stub 5756095).

    The account hash is minted by the game client and is unique per account, so it
    is strictly stronger identity evidence than the name match
    ``utils.wiseoldman._heal_stub_onto_real_twin`` relies on — and it still works
    when the real row's *name* has been corrupted, which is exactly the case that
    kept the name-keyed heal from ever firing.

    Deliberately narrow, mirroring the sibling heal: this moves the WOM id and
    nothing else. The stub's own rows (memberships, event signups) still need the
    46-table reassignment in ``scripts/merge_ghost_players.py``; the stub is left
    inert with a NULL ``wom_id`` and logged at WARNING so the pair is easy to find.
    On any failure it falls back to the stub, preserving previous behaviour.
    """
    if player_by_wom is None:
        return player_by_wom
    if not _is_temp_account_hash(player_by_wom.account_hash):
        return player_by_wom
    if player_by_hash is None or player_by_hash.player_id == player_by_wom.player_id:
        return player_by_wom
    if _is_temp_account_hash(player_by_hash.account_hash):
        return player_by_wom

    try:
        # players.wom_id is UNIQUE — the stub has to release it first.
        player_by_wom.wom_id = None
        session.flush()
        player_by_hash.wom_id = expected_wom_id
        session.commit()
    except Exception as heal_err:
        logger.log_sync(
            "warning",
            f"Failed to move wom_id {expected_wom_id} from stub "
            f"{player_by_wom.player_id} to hash owner {player_by_hash.player_id}: {heal_err}",
        )
        session.rollback()
        return player_by_wom

    logger.log_sync(
        "warning",
        f"Healed split identity by account hash: moved wom_id {expected_wom_id} from "
        f"WOM-import stub player_id={player_by_wom.player_id} to real player_id="
        f"{player_by_hash.player_id} ({player_by_hash.player_name!r}). The stub still "
        f"holds its own rows — merge it with scripts/merge_ghost_players.py.",
    )
    return player_by_hash


async def ensure_player_and_auth(session, player_name, account_hash, auth_key):
    """Ensure player exists, cache id, then auth. Returns (player, authed, user_exists)."""

    player_name = str(player_name).strip() if player_name is not None else ""
    account_hash = str(account_hash) if account_hash is not None else ""
    if not player_name:
        return None, False, False

    await asyncio.sleep(0)

    # 1) Deterministic local lookups.
    player_by_hash = None
    if account_hash:
        player_by_hash = session.query(Player).filter(Player.account_hash == account_hash).first()

    player_by_name_fast = session.query(Player).filter(Player.player_name == player_name).first()
    player = player_by_hash or player_by_name_fast
    resolved_by_name = False
    expected_wom_id = None
    wom_log_slots = None
    wom_total_level = None
    wom_ehb = None

    # Fast path: the account hash already maps to a local player whose stored
    # name matches the submitted name and whose WOM identity is already known.
    # Nothing about the identity can have changed (a name change would make the
    # names differ; a fresh install/new account would miss on hash), so there is
    # no reason to consult WOM at all. This is the hot path for ~95%+ of
    # submissions and keeps our WOM API usage proportional to *new* identities
    # rather than total submission volume.
    if (
        player_by_hash is not None
        and player_by_hash.wom_id
        and not _is_temp_account_hash(player_by_hash.account_hash)
        and normalize_player_display_equivalence(player_by_hash.player_name or "")
        == normalize_player_display_equivalence(player_name)
    ):
        player_list[player_name] = player_by_hash.player_id
        return player_by_hash, True, True

    # 2) Authoritative WOM identity lookup for this RSN.
    wom_player = None
    canonical_name = player_name
    try:
        # Release any open transaction before awaiting external API.
        try:
            session.rollback()
        except Exception:
            pass
        from utils.mirror_context import is_mirrored_submission

        # Mirrored production traffic does not get to spend WOM quota — the key
        # is shared with production, and this would double the call rate for
        # accounts dev has no reason to learn about. The hash+name hot path
        # above already covers everyone present in dev's production dump;
        # anyone else falls through to the name-only refusal below, which
        # usefully scopes mirrored traffic to accounts dev already knows.
        wom_player, resolved_name, wom_player_id, log_slots = (
            (None, None, None, None)
            if is_mirrored_submission()
            else await get_wom_user_cached(player_name)
        )
        if wom_player and wom_player_id not in (None, "", 0):
            expected_wom_id = int(wom_player_id)
            wom_log_slots = log_slots
            canonical_name = str(resolved_name or player_name)
            wom_total_level = _extract_total_level_from_wom_player(wom_player)
            wom_ehb = _extract_ehb_from_wom_player(wom_player)
    except (TypeError, ValueError):
        expected_wom_id = None

    if expected_wom_id is not None:
        # Prefer canonical WOM row over hash/name-resolved rows.
        player_by_wom = session.query(Player).filter(Player.wom_id == expected_wom_id).first()
        if player_by_wom:
            player = _heal_stub_onto_hash_owner(session, player_by_wom, player_by_hash,
                                                expected_wom_id)
        elif player is None:
            player = player_by_name_fast
            resolved_by_name = player is not None

        if player is None:
            # No local row matched: create canonical row from WOM identity.
            total_level = wom_total_level if wom_total_level is not None else 0
            player = Player(
                wom_id=expected_wom_id,
                player_name=canonical_name,
                account_hash=account_hash if account_hash else None,
                total_level=total_level,
                log_slots=wom_log_slots if wom_log_slots is not None else 0,
                ehb=wom_ehb,
            )
            created = False
            try:
                session.add(player)
                session.commit()
                created = True
            except Exception:
                session.rollback()
                player = session.query(Player).filter(Player.wom_id == expected_wom_id).first()
                if player is None:
                    return None, False, False
            if created:
                _announce_new_player(session, player)
        else:
            player, changed = _apply_authoritative_wom_identity(
                session,
                player,
                expected_wom_id,
                canonical_name=canonical_name,
                total_level=wom_total_level,
                log_slots=wom_log_slots,
                account_hash=account_hash if account_hash else None,
                ehb=wom_ehb,
            )
            if changed:
                try:
                    session.commit()
                except Exception as e:
                    debug_print("Error committing WOM identity reconciliation: " + str(e))
                    session.rollback()

        # Hash conflicts are auth failures, unless the stored hash is a temporary
        # WOM-import placeholder — in that case check_auth() handles the replacement.
        if account_hash and player and player.account_hash and str(player.account_hash) != account_hash:
            if not _is_temp_account_hash(player.account_hash):
                logger.log_sync(
                    "warning",
                    f"ensure_player_and_auth: hash mismatch for WOM {expected_wom_id}; refusing auth for {player_name}",
                )
                player_list[player_name] = player.player_id
                return player, False, True
    elif not player:
        # WOM unavailable + no deterministic local match.
        logger.log_sync(
            "warning",
            f"ensure_player_and_auth: WOM lookup unavailable for {player_name}; refusing name-only bind",
        )
        return None, False, False

    if not player:
        return None, False, False

    if expected_wom_id is not None:
        # Keep name/log slots aligned for the canonical WOM row.
        desired_name = canonical_name or player_name
        if normalize_player_display_equivalence(player.player_name or "") != normalize_player_display_equivalence(desired_name):
            player.player_name = desired_name
        if wom_log_slots is not None and wom_log_slots >= 0 and player.log_slots != wom_log_slots:
            player.log_slots = wom_log_slots
        if wom_total_level is not None and int(wom_total_level) > 0 and int(player.total_level or 0) != int(wom_total_level):
            player.total_level = int(wom_total_level)
        if wom_ehb is not None and (player.ehb is None or float(player.ehb) != float(wom_ehb)):
            player.ehb = float(wom_ehb)
        try:
            session.commit()
        except Exception:
            session.rollback()

    player_list[player_name] = player.player_id
    await asyncio.sleep(0)
    user_exists, authed = check_auth(
        player_name,
        account_hash,
        auth_key,
        session,
        resolved_player=player,
        resolved_by_name=resolved_by_name,
        expected_wom_id=expected_wom_id,
    )

    return player, authed, user_exists


unique_id_cache = {"clog": [], "drop": [], "pb": [], "ca": [], "pet": [], "quest": [], "death": [], "diary": []}
_UNIQUE_ID_CACHE_SIZE = 1000


def _remember_seen(submission_type: str, unique_id) -> None:
    """Record a GUID whose row we have just CONFIRMED exists in the database.

    Only ever called after a successful lookup — see the invariant in
    ensure_can_create().
    """
    seen = unique_id_cache.setdefault(submission_type, [])
    seen.append(unique_id)
    if len(seen) > _UNIQUE_ID_CACHE_SIZE:
        seen.pop(0)


async def ensure_can_create(session, unique_id, submission_type) -> bool:
    """Ensure no submission has already been recorded for this unique_id.

    The GUID is minted by the plugin per submission, so "a row already carries
    it" is the definition of a duplicate — regardless of how long ago that row
    was written. This check is therefore UNBOUNDED in time. It used to only
    look back one hour, which meant a submission replayed later than that could
    not see its own original and was written a second time: the 2026-08-02
    intake outage was recovered by retrying failed submissions hours later, and
    nine drops from a single 01:34 kill landed twice (06:40 and 10:28). Every
    plan for surviving the next outage — a spooler, a failover origin, draining
    a backlog — works by replaying traffic late, so late replay has to be a
    no-op rather than a way to inflate totals. The unbounded lookup is free:
    ix_drops_unique_id makes it a ~0.3 ms index hit even on the 175M-row table.

    INVARIANT: a GUID that did NOT produce a row must never read as already
    seen. The in-memory cache is strictly read-through — a GUID is recorded
    only once the row has been seen in the database (_remember_seen), never
    on the way in. The old code appended before the DB check and only ever
    evicted by size, so a submission that failed *after* being cached had its
    legitimate retry silently rejected for the life of the process — the exact
    failure an outage produces, turning one dropped submission into a
    permanently lost one.

    Returns:
        bool: True if safe to create, False if a row already exists.
    """

    await asyncio.sleep(0)
    # No GUID means no dedup key, so there is nothing to compare and the
    # submission must be allowed through. Guarding this is NOT optional now
    # that the lookup is unbounded: `unique_id == None` compiles to `IS NULL`,
    # which matches the ~200k legacy rows that carry no GUID (22k in
    # personal_best alone, and the guid-less intake paths still write ~1.2k a
    # month). Unbounded, that would match one of them every time and reject
    # every guid-less submission forever. The old one-hour window hid this by
    # only ever considering NULL rows written in the last hour.
    if not unique_id:
        return True
    if submission_type not in unique_id_cache:
        unique_id_cache[submission_type] = []
    if unique_id in unique_id_cache[submission_type]:
        return False

    def _check_existing():
        match submission_type:
            case "clog":
                return session.query(CollectionLogEntry).filter(
                    CollectionLogEntry.unique_id == unique_id,
                ).first()
            case "drop":
                # No used_api filter. The GUID identifies the submission, not
                # the transport that carried it, and webhook-path drops are
                # written used_api=False — so filtering on True made this
                # lookup blind to every one of them, and replaying any of them
                # wrote a second row instead of being a no-op. Replaying the
                # 2026-08-18 outage window duplicated 35,619 drops before this
                # was spotted. The clan_broadcast case below already carried
                # this reasoning; drops needed it too.
                return session.query(Drop).filter(
                    Drop.unique_id == unique_id,
                ).first()
            case "clan_broadcast":
                # Chat-relayed drops use a DETERMINISTIC content guid
                # ("cc:..."), so the same broadcast replayed late must match
                # its original row regardless of transport flags — no
                # used_api filter (chat rows are written used_api=False).
                return session.query(Drop).filter(
                    Drop.unique_id == unique_id,
                ).first()
            case "pb":
                return session.query(PersonalBestEntry).filter(
                    PersonalBestEntry.unique_id == unique_id,
                ).first()
            case "ca":
                return session.query(CombatAchievementEntry).filter(
                    CombatAchievementEntry.unique_id == unique_id,
                ).first()
            case "pet":
                return session.query(PlayerPet).filter(
                    PlayerPet.unique_id == unique_id,
                ).first()
            case "quest":
                return session.query(QuestCompletionEntry).filter(
                    QuestCompletionEntry.unique_id == unique_id,
                ).first()
            case "death" | "seasonal_death":
                # Deaths share one table across world types (world_type column).
                return session.query(PlayerDeath).filter(
                    PlayerDeath.unique_id == unique_id,
                ).first()
            case "diary" | "seasonal_diary":
                # Diary completions share one table across world types (world_type column).
                return session.query(DiaryCompletionEntry).filter(
                    DiaryCompletionEntry.unique_id == unique_id,
                ).first()
            case "seasonal_drop":
                # Same fix as the "drop" case above: the transport flag must
                # not narrow a GUID lookup, or webhook-path seasonal drops
                # cannot see their own original on replay.
                return session.query(SeasonalDrop).filter(
                    SeasonalDrop.unique_id == unique_id,
                ).first()
            case "seasonal_pb":
                return session.query(SeasonalPersonalBestEntry).filter(
                    SeasonalPersonalBestEntry.unique_id == unique_id,
                ).first()
            case "seasonal_clog":
                return session.query(SeasonalCollectionLogEntry).filter(
                    SeasonalCollectionLogEntry.unique_id == unique_id,
                ).first()
            case "seasonal_ca":
                return session.query(SeasonalCombatAchievementEntry).filter(
                    SeasonalCombatAchievementEntry.unique_id == unique_id,
                ).first()
            case "seasonal_pet":
                return session.query(SeasonalPlayerPet).filter(
                    SeasonalPlayerPet.unique_id == unique_id,
                ).first()
            case "seasonal_quest":
                return session.query(SeasonalQuestCompletionEntry).filter(
                    SeasonalQuestCompletionEntry.unique_id == unique_id,
                ).first()
        return None
    
    existing_entry = _check_existing()
    if existing_entry is None:
        return True
    # Proven to exist — safe to remember, so a replay storm of the same GUID
    # costs one lookup rather than one per attempt.
    _remember_seen(submission_type, unique_id)
    return False


async def ensure_npc_id_for_player(session, npc_name, player_id, player_name, use_external_session):
    """Resolve npc_id using cache, DB, or create via external API, else queue notification."""

    if not npc_name:
        return None, None
    # Consolidate multi-boss encounters up front (e.g. "Branda the Fire Queen"
    # / "Eldric the Ice King" -> "Royal Titans") so drops land on the single
    # encounter's npc row and display name, and so the >1M wiki verification
    # sees the encounter name. Must run before the cache/exact-name lookups,
    # which would otherwise resolve the individual titans' own npc_list rows.
    from utils.npc_names import canonical_encounter_name
    npc_name = canonical_encounter_name(npc_name)
    if npc_name in npc_list:
        return npc_list[npc_name], npc_name
    if ("doom of mokhaiotl" in npc_name.lower()) and ("(level" in npc_name.lower()):
        import re

        match = re.search(r"\(\s*Level\s*:??\s*(\d+)\s*\)", npc_name, flags=re.IGNORECASE)
        level_value = None
        if match:
            #print("Got a match on doom level value:", match.group(1))
            level_value = int(match.group(1))
            try:
                level_value = int(level_value)
            except Exception:
                return 14704, npc_name
            npc_name = re.sub(r"\(\s*Level\s*:??\s*(\d+)\s*\)", r"(Level \1)", npc_name, flags=re.IGNORECASE)
            #print("Parsed doom's name:", npc_name, "Level:", level_value)
            return (14707 + level_value), npc_name
        return 14707, npc_name
    npc_row = session.query(NpcList.npc_id).filter(NpcList.npc_name == npc_name).first()
    if npc_row:
        npc_list[npc_name] = npc_row.npc_id
        return npc_row.npc_id, npc_name
    # Normalized fallback (suggestion #50): sources spell the same boss
    # differently ("Tombs of Amascut: Expert Mode" vs "... Expert Mode",
    # "The Whisperer" vs "Whisperer") or name it outright differently
    # ("Crystalline Hunllef" vs "The Gauntlet"), which used to mint a second
    # npc_list row and split PBs/drops across ids. Match by variant slugs and
    # prefer the id that already has tracked data, returning the CANONICAL
    # stored name so downstream rows/embeds use one spelling.
    from sqlalchemy import bindparam

    from utils.npc_names import (
        npc_match_variants,
        npc_primary_rank_sql_expr,
        npc_primary_variants,
        npc_slug_sql_expr,
    )

    variants = npc_match_variants(npc_name)
    if variants:
        norm_row = session.execute(
            text(
                f"SELECT n.npc_id, n.npc_name, "
                f"       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
                f"              WHERE t.npc_id = n.npc_id) AS tracked "
                f"FROM npc_list n WHERE {npc_slug_sql_expr('n.npc_name')} IN :variants "
                f"ORDER BY {npc_primary_rank_sql_expr('n.npc_name')} ASC, "
                f"         tracked DESC, n.npc_id ASC LIMIT 1"
            ).bindparams(
                bindparam("variants", expanding=True),
                bindparam("primary_variants", expanding=True),
            ),
            {"variants": variants, "primary_variants": npc_primary_variants(npc_name)},
        ).first()
        if norm_row:
            canonical_id, canonical_name = int(norm_row[0]), norm_row[1]
            npc_list[npc_name] = canonical_id
            return canonical_id, canonical_name
    player_id = player_list.get(player_name)
    if player_id == 0:
        return None, npc_name
    try:
        async with osrs_api.create_client() as client:
            npc_id = await client.semantic.get_npc_id(npc_name)
        if npc_id:
            try:
                new_npc = NpcList(npc_id=npc_id, npc_name=npc_name)
                session.add(new_npc)
                session.commit()
            except Exception:
                # A concurrent worker/process minted this npc between our
                # lookup and the insert (the semantic call above is a long
                # await). Roll back the failed transaction — leaving it
                # pending would poison every later commit on this session —
                # and use the row the winner created (mirrors
                # ensure_item_for_drop's race handling).
                try:
                    session.rollback()
                except Exception:
                    pass
                existing = session.query(NpcList.npc_id).filter(NpcList.npc_id == npc_id).first()
                if not existing:
                    raise
            npc_list[npc_name] = npc_id
            return npc_id, npc_name
    except Exception:
        # Clear any failed-transaction state before falling through to the
        # new_npc notification path, which reuses this session.
        try:
            session.rollback()
        except Exception:
            pass
    notification_data = {"npc_name": npc_name, "player_name": player_name, "player_id": player_id}
    await create_notification(
        "new_npc",
        player_id,
        notification_data,
        existing_session=session if use_external_session else None,
    )
    return None, npc_name


def _safe_submitted_image_url(raw):
    """Drop an ``image_url`` a client made up.

    The intake endpoint is public, and this value is persisted and later turned
    back into a local path / fetch target by the notification sender. A
    traversal string under our own host ("…/img/../../../.env") therefore used
    to read an arbitrary server file and attach it to a Discord embed. The
    sender re-checks containment now, but nothing legitimate needs a
    client-authored URL under our host: those are written by the server after
    it downloads the file, and this function only ever sees the pre-download
    payload.
    """
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if "droptracker.io" in candidate.lower():
        return None
    return candidate


def resolve_attachment_from_drop_data(drop_data):
    """Return (attachment_url, attachment_type) based on drop_data."""

    downloaded = drop_data.get("downloaded", False)
    image_url = drop_data.get("image_url", None)
    if downloaded:
        # Server-written after a successful download — trusted.
        return image_url, "downloaded"
    if drop_data.get("attachment_type", None) is not None:
        return drop_data.get("attachment_url", None), drop_data.get("attachment_type", None)
    return _safe_submitted_image_url(image_url), None


async def download_webhook_screenshot(
    player,
    data,
    *,
    submission_type,
    entry_name,
    entry_id,
    subfolder="",
):
    """Download a Discord-webhook submission's screenshot; return its URL.

    The row-less half of :func:`attach_webhook_screenshot`, for submission
    types that keep no per-event row to hang an ``image_url`` column on.
    Experience is the one: ``PlayerExperience`` is a rolling per-player XP
    snapshot, not a log of level-ups, so there is nothing to persist the URL
    onto — the caller passes the return value straight into its notification
    payloads instead.

    ``entry_id`` is only ever part of the stored filename, so any stable
    per-submission token works; callers without a row id pass the submission
    guid. It has to be unique per submission, because collisions are resolved
    by scanning the directory for a free ``_1``, ``_2`` … suffix.

    Returns the public URL, or "" when there was nothing to download.
    """
    if data.get("downloaded"):
        # An API transport already wrote the file and set image_url.
        return ""
    attachment_url = data.get("attachment_url")
    if not attachment_url:
        return ""

    try:
        _local_path, external_url = await download_player_image(
            submission_type=submission_type,
            # Ignored by download_player_image (entry_name + entry_id name the
            # file); passed for parity with the other processors' call sites.
            file_name=entry_name,
            player=player,
            attachment_url=attachment_url,
            file_extension=get_extension_from_content_type(data.get("attachment_type")),
            entry_id=entry_id,
            entry_name=entry_name or submission_type,
            npc_name=subfolder or "",
        )
    except Exception as e:
        app_logger.log(
            log_type="error",
            data=f"Couldn't download {submission_type} image: {e}",
            app_name="core",
            description=f"{submission_type}_processor",
        )
        return ""

    return external_url or ""


async def attach_webhook_screenshot(
    session,
    player,
    entry,
    data,
    *,
    submission_type,
    entry_name,
    subfolder="",
    use_external_session=False,
):
    """Pull a Discord-webhook submission's screenshot onto ``entry``.

    Both API transports (``api/routes/webhook.py``, ``workers/webhook_consumer``)
    save the uploaded file themselves and hand the processor a finished
    ``image_url`` with ``downloaded=True``. The legacy Discord-webhook reader
    (``bots/webhook_bot.py``) cannot: it only ever sees the Discord CDN link,
    which it puts on the payload as ``attachment_url``. A processor that reads
    ``image_url`` and nothing else therefore discards the screenshot of every
    submission arriving on that transport — silently, because the row still
    saves and the notification still posts, just without an image.

    That is exactly what happened to deaths and quests: 100% of their
    webhook-path rows had no image (2,803 of 2,803 deaths over one two-day
    sample) against ~0.5% on the API paths, while clogs and CAs — which do read
    ``attachment_url`` — came through fine on the very same Discord messages.
    ``useApi`` defaults to false in the plugin, so this is the majority of
    clients, not an edge case.

    Returns the public URL, or "" when there was nothing to download. Callers
    assign it to their local ``image_url`` so the screenshot reaches the
    group-notification payload and the screenshot-required gate, not just the
    stored row.
    """
    external_url = await download_webhook_screenshot(
        player,
        data,
        submission_type=submission_type,
        entry_name=entry_name,
        # entry_id becomes part of the stored filename, so the row must already
        # be flushed — every caller adds and flushes it before getting here.
        entry_id=getattr(entry, "id", 0) or 0,
        subfolder=subfolder,
    )
    if not external_url:
        return ""

    entry.image_url = external_url
    # The row was already committed (or flushed) when it was added, so this
    # second write needs its own boundary rather than riding along on one.
    if use_external_session:
        session.flush()
    else:
        session.commit()
    return external_url


def get_player_groups_with_global(session, player: Player):
    """Fetch groups via association table, ensure global group membership."""

    player_gids = session.execute(
        text("SELECT group_id FROM user_group_association WHERE player_id = :player_id"),
        {"player_id": player.player_id},
    ).all()

    group_ids = {gid[0] for gid in player_gids} if player_gids else set()
    global_group = session.query(Group).filter(Group.group_id == 2).first()

    if global_group and global_group.group_id not in group_ids:
        player.add_group(global_group)
        session.commit()
        group_ids.add(global_group.group_id)

    if not group_ids:
        return []

    player_groups = session.query(Group).filter(Group.group_id.in_(group_ids)).all()
    return player_groups


def is_truthy_config(value):
    if value is None:
        return False
    v = str(value).strip().lower()
    return v == "true" or v == "1"


def get_group_drop_notify_settings(session, group_id):
    """Return (min_value_to_notify:int, send_stacks:bool)."""
    from utils import group_config as gc
    min_value_raw = gc.get(session, group_id, gc.MINIMUM_VALUE_TO_NOTIFY)
    min_value_to_notify = int(min_value_raw) if min_value_raw is not None else 2500000
    send_stacks = gc.is_truthy(gc.get(session, group_id, gc.SEND_STACKS_OF_ITEMS))
    return min_value_to_notify, send_stacks


def is_user_dm_enabled(session, user_id, key):
    """Whether a dm_* submission notification should be queued for this user.

    Requires both the user's per-type opt-in config AND the `dm_submissions`
    supporter entitlement (user-level premium). The send-side handler in
    services/notification_service.py re-checks the entitlement (fail closed).
    """
    cfg = (
        session.query(UserConfiguration)
        .filter(UserConfiguration.user_id == user_id, UserConfiguration.config_key == key)
        .first()
    )
    if not (cfg and is_truthy_config(cfg.config_value)):
        return False
    from db.entitlements import user_has_entitlement

    return user_has_entitlement(user_id, "dm_submissions")


async def ensure_item_by_name(session, item_name):
    if not item_name:
        return None
    item = session.query(ItemList).filter(ItemList.item_name == item_name).first()
    if item:
        return item
    try:
        async with osrs_api.create_client() as client:
            item_id = await client.semantic.get_item_id(item_name)
    except Exception:
        return None
    if not item_id:
        return None
    try:
        item = ItemList(item_name=item_name, item_id=item_id, noted=0, stackable=0, stacked=0)
        session.add(item)
        session.commit()
    except Exception:
        # Either the name is a variant of an item whose id already exists, or
        # a concurrent worker inserted it first. Roll back — the old bare
        # `return None` left the session pending-rollback, which poisoned
        # every later query in the same entry (observed 2026-08-01 00:59:
        # clog of item 29784 dead-lettered a whole entry) — and reuse the
        # existing row.
        try:
            session.rollback()
        except Exception:
            pass
        item = session.query(ItemList).filter(ItemList.item_id == item_id).first()
        if not item:
            return None
    await _ensure_item_icon(item_id)
    return item


async def ensure_player_by_name_then_auth(session, player_name, account_hash, auth_key):
    # Use the canonical identity flow so WOM stays source-of-truth for RSN -> WOM ID.
    return await ensure_player_and_auth(session, player_name, account_hash, auth_key)


stored_notifications = {}
recently_sent = []

# Group-channel notification types -> the GroupConfiguration channel key(s)
# the notification service resolves at send time. The first key is the
# per-type channel; any later key is its fallback, mirroring the exact
# resolution order in services/notification_service.py (send_X_with_session).
# Types NOT listed here (dm_* DMs, new_npc/new_item/name_change/new_player
# system notifications, events, upgrades, ...) are never gated on this.
GROUP_CHANNEL_NOTIFICATION_KEYS = {
    "drop": ("channel_id_to_post_loot",),
    # The three level-family senders all fall back to the loot channel at send
    # time (send_level_up / send_xp_milestone in notification_service.py), so
    # the enqueue gate must too — for a while "level_up" listed only the levels
    # channel here, and groups with just a drops channel had their level-ups
    # silently dropped at enqueue.
    "level_up": ("channel_id_to_post_levels", "channel_id_to_post_loot"),
    "xp_milestone": ("channel_id_to_post_levels", "channel_id_to_post_loot"),
    "total_level_milestone": ("channel_id_to_post_levels", "channel_id_to_post_loot"),
    "quest": ("channel_id_to_post_quests", "channel_id_to_post_loot"),
    "death": ("channel_id_to_post_deaths", "channel_id_to_post_loot"),
    "diary": ("channel_id_to_post_diaries", "channel_id_to_post_loot"),
    "pb": ("channel_id_to_post_pb", "channel_id_to_post_loot"),
    "ca": ("channel_id_to_post_ca", "channel_id_to_post_loot"),
    "clog": ("channel_id_to_post_clog", "channel_id_to_post_loot"),
    "pet": ("channel_id_to_post_pets",),
    "kc_milestone": ("channel_id_to_post_kc", "channel_id_to_post_loot"),
    "rank_milestone": ("channel_id_to_post_ranks", "channel_id_to_post_loot"),
}


def group_has_notification_channel(db_session, group_id, notification_type) -> bool:
    """Whether a group has a Discord channel this notification could be sent to.

    Mirrors the send-time channel resolution in
    services/notification_service.py so we stop enqueueing group-channel
    notifications that can only ever fail with "No channel configured".
    A value is considered configured when it is non-empty and not the
    legacy "0" unset sentinel. Unknown notification types (DMs, system
    notifications) always return True; lookup errors also return True so a
    transient DB issue never drops a notification.
    """
    keys = GROUP_CHANNEL_NOTIFICATION_KEYS.get(notification_type)
    if keys is None or not group_id:
        return True
    try:
        rows = (
            db_session.query(GroupConfiguration.config_key, GroupConfiguration.config_value)
            .filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key.in_(keys),
            )
            .all()
        )
        values = {row[0]: row[1] for row in rows}
    except Exception:
        return True
    return any(str(values.get(key) or "").strip() not in ("", "0") for key in keys)


def player_hidden_for_group(db_session, group_id, player_id) -> bool:
    """Whether a group leader has hidden this player from that group's surfaces.

    Leaders toggle this on the member listing (PATCH
    /api/v1/groups/{id}/hidden-players), which writes an ``ignored_players``
    row. The lootboard generators have always honoured it; the notification
    pipeline never did, so a "hidden" member kept getting announced in the
    group's Discord — the half of hiding that people actually notice.

    Scoped to group notifications only: DM/system notifications arrive here
    with no group_id and are never filtered, so hiding a player from a group
    does not cost that player their own submission DMs.

    Lookup errors return False (fail open, matching group_has_notification_channel
    and web_api.common.hidden_player_ids): a transient DB fault must not
    silently mute every group's notifications.
    """
    if not group_id or not player_id:
        return False
    try:
        return (
            db_session.query(IgnoredPlayer.id)
            .filter(
                IgnoredPlayer.group_id == group_id,
                IgnoredPlayer.player_id == player_id,
            )
            .first()
            is not None
        )
    except Exception:
        return False


def notification_blacklisted(db_session, group_id, notification_type, data) -> str | None:
    """Why this group's leaders blacklisted this notification, or ``None``.

    Leaders curate a per-group list of items and NPCs on the group settings
    page (``/api/v1/groups/{id}/notification-blacklist``). Anything on it is
    still recorded, scored and counted — it simply never reaches their Discord.
    That is the whole feature: clans want the 47th "Bones from Barrows" out of
    the channel, not out of their totals.

    Scoped to group notifications exactly like :func:`player_hidden_for_group`:
    DM/system notifications arrive with no group_id and are never filtered, so
    a group muting an item cannot cost a member their own submission DMs.

    Matching lives in ``db.notification_blacklist`` so the enqueue gate here
    and the send-side guard in ``services/notification_service.py`` cannot
    drift — the same disagreement that lost 387 clog notifications when the
    channel-fallback rule was asserted twice. Fails open on any lookup error.
    """
    if not group_id:
        return None
    try:
        from db.notification_blacklist import blacklist_reason

        return blacklist_reason(db_session, group_id, notification_type, data)
    except Exception:
        return None


def points_notify_enabled(db_session, group_id) -> bool:
    """The ``notify_points_awarded`` group toggle (default OFF, opt-in).

    When the group point system awards points for a submission the group's
    other settings would not have announced (a drop below the value minimum, a
    tier below ``min_ca_tier_to_notify``, a notify_* toggle that is off), a
    group that opted in has the processors announce it anyway. The toggle only
    widens the announcement gate: the screenshot requirement and the
    notification blacklist still apply to the forced post, and it never fires
    unless points actually landed for this group.

    Opt-in because "points landed" is not a signal of intent: every Sponsor or
    Patron group gets the default point template (1 point per clog, PB and
    easy CA), so a default-ON override amounted to ignoring notify_clogs,
    notify_pbs, notify_cas and the CA tier minimum for every paid group — the
    2026-09-01 "notifications I never enabled" reports. An absent row means
    off; only an explicit truthy value turns it on. Fails closed for the same
    reason: a config read fault must not override a leader's explicit
    notify_* settings.
    """
    try:
        from utils import group_config as gc

        raw = gc.get(db_session, group_id, "notify_points_awarded")
        return False if raw is None else gc.is_truthy(raw)
    except Exception:
        return False


def safe_death_filtered(db_session, group_id, notification_type, data) -> str | None:
    """Why this group is not told about this safe death, or ``None``.

    Groups default to announcing only deaths that cost something, matching
    Dink's client-side ``deathIgnoreSafe``; ``notify_deaths_safe`` turns the
    rest back on. Scoped to group notifications like the blacklist above, so a
    member's own death DM is never affected.

    The rule lives in ``db.death_filter`` because the send-side guard in
    ``services/notification_service.py`` has to apply exactly the same one —
    otherwise flipping the setting leaves already-queued deaths behaving by the
    old rule.
    """
    if not group_id:
        return None
    try:
        from db.death_filter import death_skip_reason

        return death_skip_reason(db_session, group_id, notification_type, data)
    except Exception:
        return None


# Notification types muted for the current task/context. Used by backfills:
# replaying an outage window has to re-record the submissions, but firing the
# matching Discord announcements hours late is confusing rather than useful.
# A ContextVar (not a module global) so it cannot leak across the concurrent
# tasks the consumers run submissions in.
_suppressed_notification_types: contextvars.ContextVar = contextvars.ContextVar(
    "suppressed_notification_types", default=frozenset()
)


@contextlib.contextmanager
def suppress_notifications(*notification_types):
    """Mute these notification types for the duration of the block.

    Types are the same strings passed to :func:`create_notification` — e.g.
    ``suppress_notifications("pb", "dm_pb")`` mutes both the group-channel
    personal-best post and its DM. Only the announcement is suppressed; the
    submission itself is still recorded, scored and counted toward events.
    """
    token = _suppressed_notification_types.set(frozenset(notification_types))
    try:
        yield
    finally:
        _suppressed_notification_types.reset(token)


# Mirrored-production-traffic context. Defined in utils/mirror_context.py (a
# leaf module) rather than here, because services/ and osrs_api/ need to ask the
# same question and importing this module from there would risk a cycle.
# Re-exported so the notification choke point and its callers read as one piece.
from utils.mirror_context import mirror_sink, sink_group_id  # noqa: E402


async def create_notification(notification_type, player_id, data, group_id=None, existing_session=None):
    """Create a notification queue entry."""

    if notification_type in _suppressed_notification_types.get():
        return None

    # Mirrored production traffic: the player's real groups are not this
    # instance's to announce in, and their configuration is not this instance's
    # to evaluate. Rerouting *here*, ahead of the channel / hidden-player /
    # blacklist gates below, is the whole point — everything downstream then
    # reads the sink group's own config, so a mirrored drop is measured against
    # the sink's minimum_value_to_notify and posted to the sink's channel rather
    # than failing against some real clan's settings.
    sink = sink_group_id()
    if sink is not None:
        # DMs have no group to reroute *to*, and the recipient is a real person
        # who did not ask to hear from the dev bot. There is no safe version of
        # this, so mirrored traffic never DMs. Every DM type is dm_-prefixed.
        if notification_type.startswith("dm_"):
            return None
        # Global (group-less) notifications — new_player, new_npc, name_change.
        # Nothing to reroute, and the sink does not want them.
        if group_id in (None, 0):
            return None
        group_id = sink

    global stored_notifications
    try:
        debug_test = int(player_id) == 1 if player_id is not None else False
    except (TypeError, ValueError):
        debug_test = False
    if group_id is not None:
        if group_id not in stored_notifications:
            stored_notifications[group_id] = []
    else:
        if 0 not in stored_notifications:
            stored_notifications[0] = []
        group_id = 0
    if len(stored_notifications[group_id]) > 100:
        while len(stored_notifications[group_id]) > 100:
            stored_notifications[group_id].pop()
    use_existing_session = existing_session is not None
    if use_existing_session:
        db_session = existing_session
    else:
        db_session = session
    # Don't enqueue group-channel notifications the send side can never
    # deliver (group has no relevant channel configured) — they'd fail with
    # "No channel configured for group X" on every send attempt.
    if group_id and not group_has_notification_channel(db_session, group_id, notification_type):
        app_logger.log(
            log_type="debug",
            data=f"Skipping {notification_type} notification for group {group_id}: no notification channel configured",
            app_name="core",
            description="create_notification",
        )
        return None
    # The group's leaders hid this member (ignored_players) — they opted the
    # player out of this group's public surfaces, Discord included.
    if player_hidden_for_group(db_session, group_id, player_id):
        app_logger.log(
            log_type="debug",
            data=f"Skipping {notification_type} notification for group {group_id}: player {player_id} is hidden for this group",
            app_name="core",
            description="create_notification",
        )
        return None
    # The group's leaders blacklisted this item, its source NPC or the place it
    # happened — they want it recorded but never announced in their Discord.
    blacklist_hit = notification_blacklisted(db_session, group_id, notification_type, data)
    if blacklist_hit:
        app_logger.log(
            log_type="debug",
            data=f"Skipping {notification_type} notification for group {group_id}: {blacklist_hit}",
            app_name="core",
            description="create_notification",
        )
        return None
    # A death that cost nothing (raid wipe, Castle Wars, POH), which groups do
    # not announce unless they asked to. Same one-rule-two-gates arrangement as
    # the blacklist above; see db.death_filter.
    safe_death_hit = safe_death_filtered(db_session, group_id, notification_type, data)
    if safe_death_hit:
        app_logger.log(
            log_type="debug",
            data=f"Skipping {notification_type} notification for group {group_id}: {safe_death_hit}",
            app_name="core",
            description="create_notification",
        )
        return None
    hashed_data = hashlib.sha256(json.dumps(data).encode()).hexdigest()
    if debug_test:
        await log_to_file(f"hashed data: {hashed_data}")
    if hashed_data in stored_notifications[group_id]:
        if debug_test:
            await log_to_file(
                f"Debug test: Notification already exists for group {group_id}, returning from create_notification without creation"
            )
            await log_to_file(f"Existing hashed data: {stored_notifications[group_id]}")
        return
    stored_notifications[group_id].append(hashed_data)
    notification = NotificationQueue(
        notification_type=notification_type,
        player_id=player_id,
        data=json.dumps(data),
        group_id=group_id if group_id != 0 else None,
        status="pending",
    )
    
    db_session.add(notification)
    if not use_existing_session:
        db_session.commit()
    notification_id = notification.id
    if debug_test:
        await log_to_file(f"Debug test: Created notification for group {group_id}: {notification_id}")
    return notification_id


async def create_player(player_name, account_hash, existing_session=None):
    """Create or reconcile a player using WOM-authoritative identity."""

    use_existing_session = existing_session is not None
    if use_existing_session:
        db_session = existing_session
    else:
        db_session = session

    account_hash = str(account_hash) if account_hash is not None else ""
    if not account_hash or len(account_hash) < 5:
        debug_print("Account hash is too short, aborting")
        return False

    player_name = str(player_name).strip() if player_name is not None else ""
    if not player_name:
        return None

    # Always resolve via WOM first so RSN -> WOM ID is authoritative.
    wom_player, resolved_name, wom_player_id, log_slots = await get_wom_user_cached(player_name)
    if not wom_player or wom_player_id in (None, "", 0):
        debug_print(f"WOM lookup failed for {player_name}; refusing non-authoritative create")
        return None

    try:
        expected_wom_id = int(wom_player_id)
    except (TypeError, ValueError):
        return None

    canonical_name = str(resolved_name or player_name)
    wom_total_level = _extract_total_level_from_wom_player(wom_player)
    wom_ehb = _extract_ehb_from_wom_player(wom_player)
    player = db_session.query(Player).filter(Player.wom_id == expected_wom_id).first()
    if not player:
        # Fallback to existing local identity rows and reconcile to authoritative WOM ID.
        player = db_session.query(Player).filter(Player.account_hash == account_hash).first()
    if not player:
        player = db_session.query(Player).filter(Player.player_name == player_name).first()

    if player is not None:
        old_name = player.player_name
        player, changed = _apply_authoritative_wom_identity(
            db_session,
            player,
            expected_wom_id,
            canonical_name=canonical_name,
            total_level=wom_total_level,
            log_slots=log_slots,
            account_hash=account_hash,
            ehb=wom_ehb,
        )
        if changed:
            try:
                db_session.commit()
            except Exception as e:
                db_session.rollback()
                debug_print("Error committing reconciled player row: " + str(e))
                return None

            if (
                old_name
                and normalize_player_display_equivalence(old_name)
                != normalize_player_display_equivalence(player.player_name)
            ):
                notification_data = {
                    "player_name": player.player_name,
                    "player_id": player.player_id,
                    "old_name": old_name,
                }
                if player.user:
                    user = db_session.query(User).filter(User.user_id == player.user_id).first()
                    if user:
                        should_dm_cfg = (
                            db_session.query(UserConfiguration)
                            .filter(
                                UserConfiguration.user_id == user.user_id,
                                UserConfiguration.config_key == "dm_account_changes",
                            )
                            .first()
                        )
                        if should_dm_cfg:
                            should_dm = str(should_dm_cfg.config_value).lower()
                            should_dm = True if should_dm in ("true", "1") else False
                            if should_dm:
                                await create_notification(
                                    "dm_name_change",
                                    player.player_id,
                                    notification_data,
                                    existing_session=db_session if use_existing_session else None,
                                )

                await create_notification(
                    "name_change",
                    player.player_id,
                    notification_data,
                    existing_session=db_session if use_existing_session else None,
                )
    else:
        total_level = wom_total_level

        new_player = Player(
            wom_id=expected_wom_id,
            player_name=canonical_name,
            account_hash=account_hash,
            total_level=total_level,
            log_slots=log_slots if log_slots is not None else 0,
            ehb=wom_ehb,
        )
        db_session.add(new_player)
        try:
            db_session.commit()
        except Exception:
            db_session.rollback()
            player = db_session.query(Player).filter(Player.wom_id == expected_wom_id).first()
            if not player:
                return None
        else:
            player = new_player
            app_logger.log(
                log_type="access",
                data=f"{canonical_name} has been created with ID {new_player.player_id} (hash: {account_hash}) ",
                app_name="core",
                description="create_player",
            )
            _announce_new_player(db_session, new_player)
            notification_data = {
                "player_name": canonical_name,
                "wom_id": expected_wom_id,
                "player_id": new_player.player_id,
                "account_hash": account_hash,
            }
            await create_notification(
                "new_player",
                new_player.player_id,
                notification_data,
                existing_session=db_session if use_existing_session else None,
            )

    player_list[player_name] = player.player_id
    if canonical_name:
        player_list[canonical_name] = player.player_id
    return player


async def try_create_player(bot: interactions.Client, player_name, account_hash):
    account_hash = str(account_hash)
    if not account_hash or len(account_hash) < 5:
        return False
    resolved_name = str(player_name).strip() if player_name is not None else ""
    player = await create_player(resolved_name, account_hash, existing_session=session)
    if not player:
        return None

    normalized_old = normalize_player_display_equivalence(resolved_name)
    normalized_new = normalize_player_display_equivalence(player.player_name or "")
    if normalized_old and normalized_new and normalized_old != normalized_new:
        old_name = resolved_name
        new_name = player.player_name
        if player.user:
            user: User = player.user
            user_discord_id = user.discord_id
            if user_discord_id:
                try:
                    user = await bot.fetch_user(user_id=user_discord_id)
                    if user:
                        embed = interactions.Embed(
                            title="Name change detected:",
                            description=f"Your account, {old_name}, has changed names to {new_name}.",
                            color="#00f0f0",
                        )
                        embed.add_field(
                            name="Is this a mistake?",
                            value="Reach out in [our discord](https://discord.gg/droptracker)",
                        )
                        embed.set_footer(global_footer)
                        await user.send(f"Hey, <@{user.discord_id}>", embed=embed)
                except Exception as e:
                    debug_print("Couldn't DM the user on a name change:" + str(e))
        from utils.messages import name_change_message

        await name_change_message(bot, new_name, player.player_id, old_name)

    player_list[resolved_name] = player.player_id
    if player.player_name:
        player_list[player.player_name] = player.player_id
    return player


async def log_to_file(data):
    file_path = "data/logs/debug_test.log"
    try:
        with open(file_path, "a") as file:
            file.write(data + "\n")
    except Exception as e:
        debug_print("Couldn't log to file: " + str(e))
        app_logger.log(log_type="error", data=f"Couldn't log to file: {e}", app_name="core", description="log_to_file")




def reraise_if_session_broken(exc: BaseException) -> None:
    """Re-raise `exc` when it means the session can no longer be committed.

    The submission processors wrap their optional side-effects (KC milestones
    and friends) in ``except Exception`` so that a failure there "must never
    cost the drop itself". That is right for a *logic* fault in the helper —
    an unknown boss, a missing config — where the session is still clean and
    the row commits normally.

    It is exactly wrong for an *infrastructure* fault. 2026-08-30: MariaDB
    timed out mid-check (``OperationalError`` 2013), the handler swallowed it,
    and the session was left needing a rollback. The next statement — the
    processor's own ``session.commit()`` — then raised ``PendingRollbackError``,
    which subclasses ``InvalidRequestError`` rather than ``DBAPIError``, so
    ``workers.webhook_consumer._is_retryable`` read it as poison and
    dead-lettered the envelope on its FIRST failure with no retries. A
    transient blip became permanent loss: 27 envelopes / 42 submissions.

    Swallowing cannot help in that case anyway — the transaction is already
    dead, so the row was never going to commit. Re-raising lets the original,
    correctly-classified error reach the consumer and take its normal retries.
    """
    try:
        from sqlalchemy.exc import (
            DBAPIError, InterfaceError, OperationalError, PendingRollbackError,
            TimeoutError as SATimeoutError,
        )
    except ImportError:
        return

    if isinstance(exc, (OperationalError, InterfaceError, SATimeoutError,
                        PendingRollbackError)):
        raise exc
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        raise exc
