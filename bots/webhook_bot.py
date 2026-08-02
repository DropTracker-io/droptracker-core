import interactions
import os
import json
import asyncio
import signal
import sys
from datetime import datetime
from dotenv import load_dotenv
from interactions.api.events import MemberUpdate, MessageCreate, MessageReactionAdd, Startup
from interactions import Embed, Intents, Message, ChannelType, OptionType, SlashContext, listen, slash_command, Permissions, slash_option
from interactions.models import Member
from db.models import Group, ItemList, PersonalBestEntry, PlayerPet, Session, Player, User, UserConfiguration
from data.submissions import adventure_log_processor, clog_processor, ca_processor, pb_processor, drop_processor, pet_processor, experience_processor
from api.services.metrics import MetricsTracker
from services.points import award_points_to_player
from services import nitro_attribution
from services import nitro_notifications
from utils.format import convert_to_ms, get_true_boss_name
from services.ticket_system import Tickets
from sqlalchemy.exc import OperationalError, DisconnectionError, InterfaceError, InternalError, DBAPIError
from db.models.base import engine as db_engine
from dotenv import load_dotenv
load_dotenv()

from utils.sentry import init_sentry
init_sentry("droptracker-webhooks")

# Provide a no-op watchdog in dev to avoid systemd usage on Windows
class _DummyWatchdog:
    def set_health_check(self, fn):
        return None
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return None
    async def notify_ready(self):
        return None

if os.getenv("STATUS") == "dev":
    SystemdWatchdog = _DummyWatchdog  # type: ignore
else:
    from monitor.sdnotifier import SystemdWatchdog
import time

target_guilds = ["1172737525069135962","900855778095800380","597397938989432842","702992720909828168","1120606216972947468"]

if os.getenv("STATUS") == "dev":
    bot_token = os.getenv("DEV_WEBHOOK_TOKEN")
else:
    bot_token = os.getenv("WEBHOOK_TOKEN")

bot = interactions.Client(token=bot_token, intents=Intents.ALL)
metrics = MetricsTracker()
watchdog = None
shutdown_event = asyncio.Event()
MAIN_WORLD_TYPE = "main"

# --- Nitro boost → group credit reconciler ------------------------------------
# Coalescing signal: set by boost-status changes on the main guild, consumed
# (debounced) by _nitro_scheduler, which is started once from on_startup.
_nitro_dirty = asyncio.Event()
_nitro_scheduler_started = False
# Discord ids currently known to be boosting, seeded by each reconcile. Used to
# fire the boost-time DM/announce exactly once per new boost (not for existing
# boosters on every restart). `_boosters_seeded` gates announcing until the
# first reconcile has populated the set.
_known_boosters: set = set()
_boosters_seeded = False
# The boost system-message history is walked in full once per process (it seeds
# every slot count the live listener missed while this bot was down); later
# passes only tail it, since new boosts arrive on the listener.
_boost_history_scanned = False
try:
    NITRO_RECONCILE_SECONDS = max(300, int(os.getenv("NITRO_RECONCILE_MINUTES", "60")) * 60)
except (TypeError, ValueError):
    NITRO_RECONCILE_SECONDS = 3600
_NITRO_DEBOUNCE_SECONDS = 30
# How often the idle scheduler looks for an admin-requested reconcile.
_NITRO_POLL_SECONDS = 60


def _normalize_world_type(raw_world_type):
    if raw_world_type is None:
        return MAIN_WORLD_TYPE
    normalized = str(raw_world_type).strip().lower()
    return normalized or MAIN_WORLD_TYPE


def _normalize_submission_type(raw_submission_type):
    normalized = str(raw_submission_type or "").strip().lower()
    match normalized:
        case "other" | "npc":
            return "drop"
        case "kill_time" | "npc_kill":
            return "personal_best"
        case "experience_update" | "experience_milestone" | "level_up" | "xp_milestone":
            # "xp_milestone" is the legacy type string older plugin builds send
            return "experience"
        case "quest_completion":
            return "quest"
        case _:
            return normalized


async def _dispatch_seasonal_submission(submission_type, embed_data, session):
    """Route seasonal submissions to their respective processors with world_type='seasonal'."""
    match submission_type:
        case "drop":
            from data.submissions import drop_processor
            return await drop_processor(embed_data, external_session=session, world_type="seasonal")
        case "collection_log":
            from data.submissions import clog_processor
            return await clog_processor(embed_data, external_session=session, world_type="seasonal")
        case "personal_best":
            from data.submissions import pb_processor
            return await pb_processor(embed_data, external_session=session, world_type="seasonal")
        case "combat_achievement":
            from data.submissions import ca_processor
            return await ca_processor(embed_data, external_session=session, world_type="seasonal")
        case "pet":
            from data.submissions import pet_processor
            return await pet_processor(embed_data, external_session=session, world_type="seasonal")
        case "quest":
            from data.submissions import quest_processor
            return await quest_processor(embed_data, external_session=session, world_type="seasonal")
        case "death":
            from data.submissions import death_processor
            return await death_processor(embed_data, external_session=session, world_type="seasonal")
        case "diary":
            from data.submissions import diary_processor
            return await diary_processor(embed_data, external_session=session, world_type="seasonal")
        case _:
            # experience and adventure_log not yet tracked for seasonal worlds
            return None

RETRYABLE_DB_ERROR_STRINGS = (
    "server has gone away",
    "connection reset",
    "lost connection",
    "packet sequence number wrong",
    "can't connect",
    "not connected",
)


def is_retryable_database_error(error: Exception) -> bool:
    err_msg = str(error).lower()
    return any(marker in err_msg for marker in RETRYABLE_DB_ERROR_STRINGS)


def rebuild_bot_client() -> None:
    """Replace the global client with a fresh instance before a restart attempt.

    interactions.Client cannot be re-started after stop(): every login() re-gathers
    the module-level commands into interactions_by_scope, which stop() never clears,
    so reusing the client raises "Duplicate Command! 0::nitro_user" forever.
    """
    global bot
    bot = interactions.Client(token=bot_token, intents=Intents.ALL)


async def shutdown_bot_client() -> None:
    """Attempt a clean shutdown to avoid leaked aiohttp sessions."""
    try:
        if hasattr(bot, "stop"):
            maybe_coro = bot.stop()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
            return
        if hasattr(bot, "close"):
            maybe_coro = bot.close()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
    except asyncio.CancelledError:
        # interactions' Client.stop() awaits its own shard task, which is being
        # cancelled as part of the stop — so the CancelledError bubbling out here
        # is expected, not a failure. If we let it escape it propagates through
        # main() and asyncio.run(), and the process exits status=1/FAILURE
        # (systemd logs a failed unit + "Unclosed client session"). Swallow it so
        # a deliberate shutdown exits 0.
        print("Bot shard task cancelled during shutdown (expected, exiting cleanly).")
    except Exception as e:
        print(f"Error while closing bot client: {e}")

# Health check function for systemd watchdog
async def health_check():
    """Comprehensive health check for the webhook bot"""
    try:
        # Check if bot is ready and connected
        if not bot.is_ready:
            return False
        
        # Check if metrics tracker is running
        if metrics is None:
            return False
        
        return True
    except Exception as e:
        print(f"Health check failed: {e}")
        return False

# Signal handlers for graceful shutdown
def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown"""
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal_handler)

@listen(MemberUpdate)
async def on_member_update(event: MemberUpdate):
    """Nudge the nitro-boost reconciler when a member's boost status changes on
    the main guild.

    The reconciler (_nitro_scheduler → run_nitro_reconcile) is the source of
    truth: it recomputes every group's boost credit from the full member list,
    so this handler only detects a ``premium_since`` transition and signals it.
    The individual-booster point award (/nitro_user, apply_nitro_boost_points)
    is a separate, manually-triggered reward and is intentionally left alone.
    """
    if not nitro_attribution.process_nitro_boosts_enabled():
        return
    try:
        if str(event.guild_id) != str(nitro_attribution.MAIN_GUILD_ID):
            return
        before = getattr(event, "before", None)
        after = getattr(event, "after", None)
        before_boost = getattr(before, "premium_since", None) if before else None
        after_boost = getattr(after, "premium_since", None) if after else None
        if before_boost == after_boost:
            return
        _nitro_dirty.set()  # boost state changed → recompute group credit

        booster_id = str(getattr(after, "id", None) or getattr(before, "id", None) or "")
        if after_boost is None:
            # Stopped boosting.
            _known_boosters.discard(booster_id)
            return
        # Started boosting: DM + announce once per new booster (once seeded, so
        # a restart doesn't re-announce everyone already boosting).
        if _boosters_seeded and booster_id and booster_id not in _known_boosters:
            _known_boosters.add(booster_id)
            # Enqueue for the MAIN bot to send the confirmation DM + channel
            # line (it shares more guilds / is more recognizable). This bot only
            # detects boosts and reconciles credit.
            try:
                await asyncio.to_thread(nitro_notifications.queue_nitro_boost, booster_id, True)
            except Exception as e:
                print(f"[nitro] failed to queue boost notification for {booster_id}: {e}")
    except Exception as e:
        print(f"[nitro] member update handling failed: {e}")

@slash_command(name="nitro_user",
                   description="Award nitro boost points to a user")
async def nitro_user_cmd(ctx: SlashContext):
    author = ctx.author
    author_roles = author.roles
    can_award = False
    if 1342871954885050379 in [role.id for role in author_roles]:
        can_award = True
    if 1176291872143052831 in [role.id for role in author_roles]:
        can_award = True
    if not can_award:
        embed = Embed(description=":warning: You do not have permission to use this command.")
        await ctx.send(embeds=[embed])
        return

    # Use local session to avoid conflicts
    await apply_nitro_boost_points(ctx.author.id)
    return await ctx.send(f"Nitro boost points awarded to {ctx.author.username}.", ephemeral=True)



async def apply_nitro_boost_points(user_id: int, session_to_use = None):
    # close() in a finally: an exception here used to leak the session — and a
    # leaked session keeps its connection checked out with an open transaction
    # that pool reset can never rescue.
    local_session = session_to_use if session_to_use else Session()
    try:
        user = local_session.query(User).filter(User.discord_id == user_id).first()
        if not user:
            print(f"Nitro boost: no user found for discord id {user_id}, skipping award")
            return
        user_players = local_session.query(Player).filter(Player.user_id == user.user_id).all()
        if not user_players:
            print(f"Nitro boost: user {user.user_id} has no players, skipping award")
            return
        print(f"Awarding nitro boost to {user_players[0].player_name}...")
        award_points_to_player(player_id=user_players[0].player_id, amount=250, source='Nitro Boost Upgrade',expires_in_days=60,session=local_session)
        if not session_to_use:
            # award_points_to_player only flushes when it is handed a session —
            # the caller owns the commit. Without this the close() below rolled
            # the credit straight back while the command still replied
            # "points awarded".
            local_session.commit()
    finally:
        if not session_to_use:
            local_session.close()


@listen(MessageCreate)
async def on_boost_system_message(event: MessageCreate):
    """Record a boost's slot count from Discord's own boost system message.

    This is the ONLY per-member slot signal Discord offers: ``content`` on a
    type 8/9/10/11 message is the booster's cumulative boost count. It matters
    most for the case member-list enumeration cannot see at all — a member
    placing a second boost, which leaves ``premium_since`` untouched so
    MemberUpdate never fires.
    """
    if not nitro_attribution.process_nitro_boosts_enabled():
        return
    try:
        message = event.message
        if message is None or str(getattr(message, "guild", None) and message.guild.id) != str(
            nitro_attribution.MAIN_GUILD_ID
        ):
            return
        # Reuse the pure parser so bot + history scan agree on the semantics.
        parsed = nitro_attribution.boost_count_from_message({
            "type": int(message.type),
            "content": message.content,
            "author": {"id": str(message.author.id)} if message.author else {},
        })
        if parsed is None:
            return
        booster_id, slots = parsed

        def _db():
            with Session() as s:
                ok = nitro_attribution.record_observed_boost_count(s, booster_id, slots)
                s.commit()
                return ok

        recorded = await asyncio.to_thread(_db)
        print(f"[nitro] boost message: {booster_id} -> {slots} slot(s) (recorded={recorded})")
        _nitro_dirty.set()  # re-credit with the new slot count
    except Exception as e:
        print(f"[nitro] boost message handling failed: {e}")


async def run_nitro_reconcile(deep_scan: bool = False):
    """Enumerate current boosters of the main guild and reconcile per-group
    nitro credit legs. DB work runs off the event loop.

    Three signals feed this (see services/nitro_attribution.py): the member list
    (who boosts), the boost system messages (how many slots each placed), and
    ``premium_subscription_count`` (the authoritative total, used as the ceiling
    and to report slots nothing could attribute).
    """
    global _boosters_seeded, _boost_history_scanned
    boosters = await nitro_attribution.fetch_booster_discord_ids(bot.http)
    state = await nitro_attribution.fetch_guild_boost_state(bot.http)

    observed = {}
    if state.get("boost_messages_enabled"):
        # Full history once per process (seeds everything the live listener
        # missed while we were down); a shallow tail on later passes.
        full = deep_scan or not _boost_history_scanned
        observed = await nitro_attribution.fetch_boost_message_counts(
            bot.http, state["system_channel_id"], max_pages=60 if full else 3
        )
        if full:
            _boost_history_scanned = True
    else:
        print("[nitro] boost system messages are disabled — slot counts rely on overrides.")

    def _db():
        with Session() as s:
            return nitro_attribution.run_reconcile(
                s, boosters, observed_counts=observed, guild_total=state.get("total")
            )

    stats = await asyncio.to_thread(_db)
    # Seed the known-booster set so the boost-time DM/announce fires only for
    # genuinely new boosters (self-heals any adds/removes the events missed).
    _known_boosters.clear()
    _known_boosters.update(str(b) for b in boosters)
    _boosters_seeded = True
    print(f"[nitro] reconcile complete: {stats}")
    if stats.get("unattributed"):
        print(
            f"[nitro] WARNING: {stats['unattributed']} boost slot(s) of "
            f"{stats.get('guild_total')} could not be attributed to a member — "
            f"assign them on /admin/nitro-boosts."
        )
    return stats


def _nitro_reconcile_requested() -> bool:
    """True (and clears) when the admin dashboard asked for an immediate pass.

    An override written by the web API would otherwise wait out the hour-long
    tick before the group's credit moved.
    """
    try:
        from utils.redis import RedisClient

        client = RedisClient().client
        return bool(client and client.getdel(nitro_attribution.RECONCILE_REQUEST_KEY))
    except Exception:
        return False


async def _nitro_scheduler():
    """Periodic + event-triggered nitro-boost reconciler. The interval bounds
    staleness; boost-change events trigger an earlier (debounced) pass."""
    if not nitro_attribution.process_nitro_boosts_enabled():
        print("[nitro] PROCESS_NITRO_BOOSTS disabled — reconciler not started.")
        return
    await asyncio.sleep(60)  # let the gateway connect + member cache warm
    while not shutdown_event.is_set():
        try:
            await run_nitro_reconcile()
        except Exception as e:
            print(f"[nitro] reconcile failed: {e}")
        try:
            # Poll for an admin-requested pass while waiting out the interval,
            # so a manual override lands in minutes rather than within the hour.
            deadline = time.monotonic() + NITRO_RECONCILE_SECONDS
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    await asyncio.wait_for(
                        _nitro_dirty.wait(), timeout=min(_NITRO_POLL_SECONDS, remaining)
                    )
                    await asyncio.sleep(_NITRO_DEBOUNCE_SECONDS)  # coalesce a burst
                    break
                except asyncio.TimeoutError:
                    if await asyncio.to_thread(_nitro_reconcile_requested):
                        break
        finally:
            _nitro_dirty.clear()


# Add retry decorator for database operations
def retry_on_database_error(max_retries=3, delay=1):
    """Decorator to retry database operations on connection failures"""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (OperationalError, DisconnectionError, InterfaceError, InternalError, DBAPIError) as e:
                    last_exception = e
                    if is_retryable_database_error(e):
                        retry_delay = delay * (attempt + 1)
                        print(f"Retryable database error on attempt {attempt + 1}: {e}. Retrying in {retry_delay}s...")
                        try:
                            # Drop pooled connections so retries do not reuse a corrupted socket.
                            db_engine.dispose()
                        except Exception as dispose_error:
                            print(f"Failed to dispose DB engine during retry: {dispose_error}")
                        if attempt < max_retries - 1:  # Don't sleep on the last attempt
                            await asyncio.sleep(retry_delay)
                        continue
                    else:
                        raise  # Re-raise if it's not a connection issue
                except Exception as e:
                    # For non-database errors, don't retry
                    raise
            
            # If we get here, all retries failed
            print(f"All {max_retries} database retry attempts failed")
            raise last_exception
        return wrapper
    return decorator



@retry_on_database_error(max_retries=3, delay=1)
async def process_submission_with_session(submission_type, embed_data):
    """Process a submission with a fresh database session"""
    world_type = _normalize_world_type(embed_data.get("world_type"))
    embed_data["world_type"] = world_type
    normalized_submission_type = _normalize_submission_type(submission_type)
    session = Session()
    try:
        success = False
        if world_type == "seasonal":
            from services.seasonal_state import is_seasonal_active
            if is_seasonal_active():
                result = await _dispatch_seasonal_submission(normalized_submission_type, embed_data, session)
            else:
                # Global kill switch (admin panel): skip seasonal processing.
                result = None
            success = True
        elif world_type != MAIN_WORLD_TYPE:
            result = None
            success = True
        elif normalized_submission_type == "collection_log":
            print("Received collection log submission from non-api user")
            result = await clog_processor(embed_data, external_session=session)
            success = True
        elif normalized_submission_type == "combat_achievement":
            result = await ca_processor(embed_data, external_session=session)
            success = True
        elif normalized_submission_type == "personal_best":
            result = await pb_processor(embed_data, external_session=session)
            success = True
        elif normalized_submission_type == "drop":
            result = await drop_processor(embed_data, external_session=session)
            success = True
        elif normalized_submission_type == "pet":
            result = await pet_processor(embed_data, external_session=session)
            success = True
        elif normalized_submission_type == "adventure_log":
            result = await adventure_log_processor(embed_data, external_session=session)
            success = True
        elif normalized_submission_type == "experience":
            # Level-ups and experience_update snapshots from non-API users;
            # snapshots keep event xp_target baselines/deltas advancing
            # (events gate non-API credit via their submission_policy).
            result = await experience_processor(embed_data, external_session=session)
            success = True
        else:
            result = None
        
        # Commit the session if everything succeeded
        session.commit()
        try:
            metrics.record_request(normalized_submission_type, success, app="webhook_bot")
            #print(f"Recorded request: {submission_type} {success}")
        except Exception:
            #print(f"Error recording request: {submission_type} {success}")
            pass
        return result
        
    except Exception as e:
        # Rollback on any error
        try:
            session.rollback()
        except Exception as rollback_error:
            print(f"Rollback failed for {submission_type}: {rollback_error}")
        #print(f"Error processing {submission_type}: {e}")
        try:
            metrics.record_request(normalized_submission_type, False, app="webhook_bot")
        except Exception:
            pass
        raise
    finally:
        # Always close the session
        session.close()
        # Also release the module-global scoped session: helpers deep in the
        # processor chain (db.ops, utils.embeds, ...) fall back to it for
        # reads, which autobegins a transaction this handler never ends. Left
        # alone, that idle transaction (and its metadata locks) survives until
        # the next unrelated commit on this thread — or forever (same failure
        # mode as the 2026-07-16 player-updates 20h idle-trx incident).
        # remove() closes the thread-local session and rolls back any open
        # read transaction; the next fallback use transparently gets a fresh
        # one, so per-cycle cleanup here is safe.
        try:
            from db.models import session as _scoped_session
            _scoped_session.remove()
        except Exception:
            pass

@interactions.listen(MessageCreate)
async def on_message_create(event: MessageCreate):
    def embed_to_dict(embed: Embed):
        if embed.fields:
            return {f.name: f.value for f in embed.fields}
        return {}
    
    bot: interactions.Client = event.bot
    if not bot.is_ready:
        return
    
    if isinstance(event, Message):
        message = event
    else:
        message = event.message
        
    if message.author.system:  # or message.author.bot:
        return
    if message.author.id == bot.user.id:
        return
    if message.channel.type == ChannelType.DM or message.channel.type == ChannelType.GROUP_DM:
        return
    channel_id = message.channel.id
                    
    if str(message.guild.id) in (str(guild) for guild in target_guilds) or message.guild.id == os.getenv("DISCORD_GUILD_ID"):
        for embed in message.embeds:
            embed_data = embed_to_dict(embed)
            if message.attachments:
                for attachment in message.attachments:
                    if attachment.url:
                        embed_data['attachment_url'] = attachment.url
                        embed_data['attachment_type'] = attachment.content_type
                        
            field_names = [field.name for field in embed.fields]
            if embed_data:
                field_values = [field.value.lower().strip() for field in embed.fields]
                if "source_type" in field_names and "loot chest" in field_values:
                    ## Skip pvp
                    continue
                    
                # Transport flag for the DB rows only (API vs webhook intake).
                # The events engine no longer derives plugin-vs-manual from
                # this: processors stamp the event envelope's used_api from
                # intake_source, so webhook-path plugin traffic still counts
                # as plugin under confirm_non_api / api_only policies.
                embed_data['used_api'] = False
                
                try:
                    if "collection_log" in field_values:
                        await process_submission_with_session("collection_log", embed_data)
                        continue
                    elif "combat_achievement" in field_values:
                        await process_submission_with_session("combat_achievement", embed_data)
                        continue
                    elif "npc_kill" in field_values or "kill_time" in field_values:
                        await process_submission_with_session("personal_best", embed_data)
                        continue
                    elif embed.title and "received some drops" in embed.title or "drop" in field_values:
                        await process_submission_with_session("drop", embed_data)
                        continue
                    elif ("experience_update" in field_values or "experience_milestone" in field_values
                          or "level_up" in field_values or "xp_milestone" in field_values):
                        await process_submission_with_session(embed_data.get("type", "experience_update"), embed_data)
                        continue
                    elif "quest_completion" in field_values:
                        # await quest_processor(embed_data)
                        continue
                    elif "pet" in field_values and "pet_name" in field_names:
                        await process_submission_with_session("pet", embed_data)
                        continue
                    elif "adventure_log" in field_values:
                        await process_submission_with_session("adventure_log", embed_data)
                        continue
                        
                except Exception as e:
                    print(f"Failed to process submission after retries: {e}")
                    # Continue processing other embeds even if one fails
    else:
        print(f"Message is not in the target guilds: {message.guild.id}")

        
@interactions.listen(Startup)
async def on_startup(event: Startup):
    
    # Load extensions first (they don't require database)
    try:
        bot.load_extension("services.ticket_system")
    except Exception as e:
        print(f"Error loading extensions: {e}")
    try:
        bot.load_extension("services.suggestion_sync")
    except Exception as e:
        print(f"Error loading suggestion sync: {e}")
    # Start the nitro-boost reconciler once (Startup can re-fire on reconnects).
    global _nitro_scheduler_started
    if not _nitro_scheduler_started:
        _nitro_scheduler_started = True
        asyncio.create_task(_nitro_scheduler())
    # Then handle database operations with proper session management
    player_count = 0
    local_session = Session()
    try:
        
        player_count = local_session.query(Player.player_id).count()
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name=f" ~{player_count} players", type=interactions.ActivityType.WATCHING))
    except (OperationalError, DisconnectionError) as e:
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name="DropTracker Bot", type=interactions.ActivityType.WATCHING))
    except Exception as e:
        print(f"Unexpected error during startup: {e}")
        await bot.change_presence(status=interactions.Status.ONLINE,
                            activity=interactions.Activity(name="DropTracker Bot", type=interactions.ActivityType.WATCHING))
    finally:
        local_session.close()
    
    

@interactions.listen(MessageReactionAdd)
async def on_message_reaction_add(event: MessageReactionAdd):
    if os.getenv("SHOULD_PROCESS_REACTIONS") == "false":
        return
    if event.message.id == os.getenv("DISCORD_MESSAGE_REACTION_ROLE_MESSAGE_ID"):
        if event.emoji.id == os.getenv("DISCORD_MESSAGE_REACTION_ROLE_EMOJI_ID"):
            emoji_user = event.author
            dt_guild = bot.get_guild(os.getenv("DISCORD_GUILD_ID"))
            member = dt_guild.get_member(member_id=emoji_user.id)
            if member:
                await member.add_role(role=os.getenv("DISCORD_MESSAGE_REACTION_ROLE_ROLE_ID"))
            return

async def main():
    """Main function with systemd watchdog integration"""
    global watchdog
    
    # Setup signal handlers
    setup_signal_handlers()
    
    # Initialize systemd watchdog
    watchdog = SystemdWatchdog()
    watchdog.set_health_check(health_check)
    
    try:
        async with watchdog:
            # Notify systemd that we're ready
            await watchdog.notify_ready()
            print("Systemd watchdog initialized and ready notification sent")

            shutdown_wait_task = asyncio.create_task(shutdown_event.wait())
            consecutive_failures = 0

            while not shutdown_event.is_set():
                started_at = time.monotonic()
                bot_task = asyncio.create_task(bot.astart(token=bot_token))

                done, pending = await asyncio.wait(
                    [bot_task, shutdown_wait_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                if shutdown_wait_task in done:
                    print("Shutdown requested, stopping bot...")
                    if not bot_task.done():
                        bot_task.cancel()
                    try:
                        await bot_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as shutdown_error:
                        print(f"Bot raised while shutting down: {shutdown_error}")
                    break

                bot_error = None
                if bot_task.done():
                    try:
                        bot_error = bot_task.exception()
                    except asyncio.CancelledError:
                        bot_error = None

                # A long stable run means the previous failures were transient.
                if time.monotonic() - started_at > 120:
                    consecutive_failures = 0
                consecutive_failures += 1
                restart_delay = min(60, 2 ** min(consecutive_failures, 5))
                if bot_error:
                    print(f"Bot crashed unexpectedly (attempt {consecutive_failures}): {bot_error}")
                else:
                    print(f"Bot task exited unexpectedly without exception (attempt {consecutive_failures}).")

                await shutdown_bot_client()
                if shutdown_event.is_set():
                    break
                print(f"Restarting bot in {restart_delay}s...")
                await asyncio.sleep(restart_delay)
                rebuild_bot_client()

            if not shutdown_wait_task.done():
                shutdown_wait_task.cancel()
                try:
                    await shutdown_wait_task
                except asyncio.CancelledError:
                    pass

            await shutdown_bot_client()
            print("Webhook bot shutting down gracefully...")
            
    except KeyboardInterrupt:
        print("Received keyboard interrupt")
    except Exception as e:
        print(f"Fatal error in main: {e}")
        raise
    finally:
        print("Webhook bot cleanup completed")

if __name__ == "__main__":
    asyncio.run(main())