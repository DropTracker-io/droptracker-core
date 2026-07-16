import asyncio
import json
import os
from urllib.parse import quote
import shutil
from datetime import datetime, timedelta
import interactions
import aiohttp
import aiofiles
from sqlalchemy import text
from db.models import (
    ItemList,
    NotificationQueue,
    NpcList,
    PersonalBestEntry,
    User,
    UserConfiguration,
    get_current_partition,
    session,
    Player,
    Group,
    GroupConfiguration,
)
from db.ops import DatabaseOperations, associate_player_ids, get_formatted_name
from db.entitlements import has_custom_embeds
from utils.redis import redis_client
from utils.messages import confirm_new_npc, confirm_new_item, name_change_message, new_player_message
from utils.format import format_number, replace_placeholders, convert_from_ms
from db.app_logger import AppLogger
from utils import osrs_api
from services.contribution_notifications import format_money
from services.event_notifications import (
    EVENT_NOTIFICATION_TYPES,
    effective_message_config,
    event_ping_role_ids,
    event_url,
    load_event_channels,
    ping_content,
    resolve_event_channel,
    should_send_event_message,
)
from utils.wiseoldman import fetch_group_members
from services.redis_updates import get_player_list_loot_sum, loot_tracker
from db.models.video_upload import VideoUpload
from utils.video_storage import (
    VIDEO_LOCAL_DELETE_AFTER_NOTIFY,
    backend_for_video_record,
    delete_object,
    get_public_video_url,
)

app_logger = AppLogger()
global_footer = os.getenv('DISCORD_MESSAGE_FOOTER')
db = DatabaseOperations()

# Item/NPC/metric icon assets are served from /img and live on this same box,
# so a task-tile icon URL can be existence-checked locally before it is handed
# to Discord (missing file -> no thumbnail rather than a broken image).
IMG_BASE = "https://www.droptracker.io/img"
STATIC_IMG_DIR = "/store/droptracker/disc/static/assets/img"

# Monetary contribution announcements (queued by web_api/payments.py via
# services/contribution_notifications.py). Channel defaults to the same
# global supporters channel the legacy group_upgrade notifications used.
CONTRIBUTION_CHANNEL_ID = int(os.getenv("DISCORD_CONTRIBUTION_CHANNEL_ID", "1490419196012793866"))
SUPPORTER_EMOJI = "<:supporter:1263827303712948304>"
DROPTRACKER_EMOJI = "<a:droptracker:1346787143778963497>"
BRAND_THUMBNAIL = "https://www.droptracker.io/img/droptracker-small.gif"
CONTRIBUTION_COLOR = "#00f0f0"

# Direct-message queue types. Submission DMs (everything except dm_name_change)
# are a supporter perk gated on the `dm_submissions` user entitlement; queueing
# is opt-in via user_configurations `dm_*` keys (see data/submissions/*).
SUBMISSION_DM_TYPES = frozenset({
    'dm_drop', 'dm_pb', 'dm_ca', 'dm_clog', 'dm_pet',
    'dm_quest', 'dm_death', 'dm_diary', 'dm_level_up', 'dm_name_change',
})


# Removed global tracking dictionaries - now using database-based tracking via NotifiedSubmission table

class NotificationService:
    def __init__(self, bot: interactions.Client, db_ops: DatabaseOperations):
        self.bot = bot
        self.db_ops = db_ops
        self.notified_users = []
        self.running = False
        self._processing_lock = asyncio.Lock()
        # Background task that runs the processing loop
        self._task = None
        # Configurable sleep interval (in seconds)
        self.sleep_interval = 3
        # Statistics tracking
        self.processed_count = 0
        self.last_processed_at = None
        # Daily notification_queue retention prune bookkeeping
        self._last_queue_prune_at = None
        self._video_notif_dir = os.getenv("VIDEO_NOTIFICATION_DIR", "/tmp/droptracker-video-notifs")
        self._video_notif_ttl_seconds = int(os.getenv("VIDEO_NOTIFICATION_TTL_SECONDS", "1800"))  # 30 min
        self._video_notif_max_bytes = int(os.getenv("VIDEO_NOTIFICATION_MAX_BYTES", str(5 * 1024 * 1024)))  # 5MB

    @staticmethod
    def _coerce_int(value):
        """Best-effort integer parsing for mixed payload values."""
        if value in (None, ""):
            return None
        try:
            return int(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _format_group_points_members(value) -> str:
        """Render awarded member details into a concise placeholder-friendly string."""
        if not isinstance(value, list) or not value:
            return ""
        parts = []
        for member in value:
            if not isinstance(member, dict):
                continue
            name = str(member.get("player_name") or member.get("player_id") or "Unknown")
            awarded = member.get("points_awarded", 0)
            total = member.get("current_points", 0)
            parts.append(f"{name} (+{awarded}, total {total})")
        return ", ".join(parts)

    @staticmethod
    def _is_same_member_as_target(member: dict, target_player_name: str, target_player_id) -> bool:
        """Return True when an awarded member matches the notification target player."""
        if not isinstance(member, dict):
            return False

        member_id = member.get("player_id")
        target_id = target_player_id
        if member_id not in (None, "") and target_id not in (None, ""):
            try:
                return int(str(member_id).strip()) == int(str(target_id).strip())
            except (TypeError, ValueError):
                pass

        member_name = str(member.get("player_name") or "").strip().lower()
        target_name = str(target_player_name or "").strip().lower()
        return bool(member_name and target_name and member_name == target_name)

    @staticmethod
    def _strip_group_points_placeholders(text: str) -> str:
        if text is None:
            return text
        cleaned = str(text)
        for placeholder in (
            "{group_points_awarded}",
            "{group_points_receiver_total}",
            "{group_points_member_count}",
            "{group_points_members_awarded}",
        ):
            cleaned = cleaned.replace(placeholder, "")
        while "  " in cleaned:
            cleaned = cleaned.replace("  ", " ")
        return cleaned.strip()

    def _finalize_group_points_embed(self, embed: interactions.Embed) -> interactions.Embed:
        """Remove unresolved group-point placeholders and empty fields safely."""
        if not embed:
            return embed

        if getattr(embed, "title", None) and "{group_points_" in embed.title:
            embed.title = self._strip_group_points_placeholders(embed.title) or None
        if getattr(embed, "description", None) and "{group_points_" in embed.description:
            embed.description = self._strip_group_points_placeholders(embed.description) or None
        if getattr(embed, "footer", None) and getattr(embed.footer, "text", None):
            if "{group_points_" in embed.footer.text:
                embed.footer.text = self._strip_group_points_placeholders(embed.footer.text) or ""

        if embed.fields:
            kept_fields = []
            for field in embed.fields:
                field_name = getattr(field, "name", "") or ""
                field_value = getattr(field, "value", "") or ""
                combined = f"{field_name} {field_value}"
                if "{group_points_" in combined:
                    # Placeholder not replaced -> data was not provided, omit this field.
                    continue
                if str(field_value).strip() == "":
                    continue
                kept_fields.append(field)
            embed.fields = kept_fields
        return embed

    def _group_points_placeholder_map(self, data: dict) -> dict:
        members = data.get("group_points_members_awarded") or []
        members_text = self._format_group_points_members(members)
        points_awarded = self._coerce_int(data.get("group_points_awarded")) or 0
        receiver_total = self._coerce_int(data.get("group_points_receiver_total")) or 0
        member_count = self._coerce_int(data.get("group_points_member_count"))
        if member_count is None:
            member_count = len(members) if isinstance(members, list) else 0

        target_player_name = data.get("player_name")
        target_player_id = data.get("player_id")
        suppress_members_awarded = (
            isinstance(members, list)
            and len(members) == 1
            and self._is_same_member_as_target(members[0], target_player_name, target_player_id)
        )

        values = {}
        # Only show award-related placeholders when points were actually awarded.
        if points_awarded > 0:
            values["{group_points_awarded}"] = str(points_awarded)
        if member_count > 0 and members_text and not suppress_members_awarded:
            values["{group_points_member_count}"] = str(member_count)
            values["{group_points_members_awarded}"] = members_text
        # Current total can be shown even if this event awarded zero points.
        if receiver_total > 0:
            values["{group_points_receiver_total}"] = str(receiver_total)
        return values

    def _plugin_version_placeholder_map(self, data: dict) -> dict:
        return {"{plugin_version}": str(data.get("plugin_version") or "")}

    def _build_default_quest_embed(self, data: dict, player_name: str, player_id: int, video_url: str = "") -> interactions.Embed:
        """
        Build a default quest embed when no DB-backed template exists.
        """
        quest_name = str(data.get("quest_name") or "Unknown quest")
        quests_completed = self._coerce_int(data.get("quests_completed"))
        total_quests = self._coerce_int(data.get("total_quests"))
        completion_percentage = str(data.get("completion_percentage") or "N/A")
        quest_points = self._coerce_int(data.get("quest_points"))
        total_quest_points = self._coerce_int(data.get("total_quest_points"))
        qp_percentage = str(data.get("qp_percentage") or "N/A")
        submitted_at = self._coerce_int(data.get("timestamp"))

        player_link = f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)"
        embed = interactions.Embed(
            title="Quest Completed",
            description=f"{player_link} completed **{quest_name}**.",
            color="#5A8DEE",
        )
        embed.add_field(
            name="Quest Progress",
            value=f"`{quests_completed if quests_completed is not None else '?'}`/`{total_quests if total_quests is not None else '?'}` ({completion_percentage})",
            inline=True,
        )
        embed.add_field(
            name="Quest Points",
            value=f"`{quest_points if quest_points is not None else '?'}`/`{total_quest_points if total_quest_points is not None else '?'}` ({qp_percentage})",
            inline=True,
        )
        if video_url:
            embed.add_field(name="Video", value=f"[Watch clip]({video_url})", inline=False)
        embed.set_footer(global_footer)
        return embed

    def _build_default_death_embed(self, data: dict, player_name: str, player_id: int, video_url: str = "") -> interactions.Embed:
        """
        Build a default death embed when no DB-backed template exists.
        """
        source = str(data.get("source") or "").strip()
        location = str(data.get("location") or "").strip()
        region_id = self._coerce_int(data.get("region_id"))

        player_link = f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)"
        embed = interactions.Embed(
            title="Player Death",
            description=f"{player_link} has died.",
            color="#B23B3B",
        )
        if source:
            embed.add_field(name="Killed By", value=source, inline=True)
        if location:
            embed.add_field(name="Location", value=location, inline=True)
        elif region_id is not None:
            embed.add_field(name="Region", value=f"`{region_id}`", inline=True)
        if video_url:
            embed.add_field(name="Video", value=f"[Watch clip]({video_url})", inline=False)
        embed.set_footer(global_footer)
        return embed

    def _build_default_diary_embed(self, data: dict, player_name: str, player_id: int, video_url: str = "") -> interactions.Embed:
        """
        Build a default achievement diary embed when no DB-backed template exists.
        """
        diary_name = str(data.get("diary_name") or "Unknown area")
        diary_tier = str(data.get("diary_tier") or "").strip()
        diary_label = f"{diary_tier} {diary_name}".strip()

        player_link = f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)"
        embed = interactions.Embed(
            title="Achievement Diary Completed",
            description=f"{player_link} completed the **{diary_label}** diary.",
            color="#5A8DEE",
        )
        if video_url:
            embed.add_field(name="Video", value=f"[Watch clip]({video_url})", inline=False)
        embed.set_footer(global_footer)
        return embed

    def _maybe_get_video_url(self, db_session, data: dict) -> str:
        """
        Best-effort resolution of a public video URL for notifications.

        Order of precedence:
        1) explicit video_url in notification data
        2) lookup via video_key in VideoUpload (only if processed)
        """
        try:
            direct = data.get("video_url")
            if direct:
                return str(direct)
            video_key = data.get("video_key")
            if not video_key:
                return ""
            rec = (
                db_session.query(VideoUpload)
                .filter(VideoUpload.video_key == video_key)
                .first()
            )
            if rec and rec.status == "processed" and rec.final_key:
                return get_public_video_url(
                    rec.final_key,
                    backend=backend_for_video_record(rec),
                )
        except Exception:
            pass
        return ""

    async def _cleanup_processed_local_video_after_send(self, db_session, data: dict) -> None:
        """
        Best-effort cleanup of processed local videos after a successful notification send.
        """
        if not VIDEO_LOCAL_DELETE_AFTER_NOTIFY:
            return
        try:
            video_key = data.get("video_key")
            if not video_key:
                return
            rec = (
                db_session.query(VideoUpload)
                .filter(VideoUpload.video_key == video_key)
                .first()
            )
            if not rec or rec.status != "processed" or not rec.final_key:
                return
            if backend_for_video_record(rec) != "local":
                return
            await delete_object(rec.final_key, backend="local")
        except Exception:
            # Best effort only
            return

    async def _cleanup_old_video_attachments(self) -> None:
        """Delete old temp MP4 files for notification attachments."""
        try:
            os.makedirs(self._video_notif_dir, exist_ok=True)
            now_ts = datetime.now().timestamp()
            for name in os.listdir(self._video_notif_dir):
                if not name.endswith(".mp4"):
                    continue
                path = os.path.join(self._video_notif_dir, name)
                try:
                    st = os.stat(path)
                    if now_ts - st.st_mtime > self._video_notif_ttl_seconds:
                        os.remove(path)
                except FileNotFoundError:
                    continue
                except Exception:
                    continue
        except Exception:
            # Best effort cleanup only
            return

    async def _resolve_image_attachment(self, image_url, notification_id) -> tuple["interactions.File | None", "str | None"]:
        """One image URL -> an attachable ``interactions.File``.

        droptracker.io URLs map straight to their file under static/assets
        (no HTTP round-trip). Any other http(s) URL — e.g. the Discord CDN
        URL a low-value non-API drop used to carry — is fetched to a temp
        file so the screenshot still ships with the embed instead of being
        silently discarded (the pre-2026-07-15 behavior).

        Returns ``(attachment, temp_path)``; ``temp_path`` is only set for
        remote fetches and must be deleted by the caller after sending.
        Best-effort: any failure returns ``(None, None)`` (embed sends
        without an image, never fails the notification)."""
        if not image_url or not isinstance(image_url, str):
            return None, None
        try:
            if "droptracker.io" in image_url:
                local_path = image_url.replace(
                    "https://www.droptracker.io/img/",
                    "/store/droptracker/disc/static/assets/img/",
                )
                if os.path.exists(local_path):
                    return interactions.File(local_path), None
                print(f"Debug - Image file not found at: {local_path}")
                return None, None
            if not image_url.startswith(("http://", "https://")):
                return None, None
            # Remote (non-hosted) image: fetch to a temp file. 10 MB cap —
            # plugin screenshots are a few hundred KB.
            os.makedirs(self._video_notif_dir, exist_ok=True)
            ext = ".png" if ".png" in image_url.lower() else ".jpg"
            local_path = os.path.join(self._video_notif_dir, f"notif_img_{notification_id}{ext}")
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session_http:
                async with session_http.get(image_url) as resp:
                    if resp.status != 200:
                        return None, None
                    data = await resp.content.read(10 * 1024 * 1024 + 1)
                    if not data or len(data) > 10 * 1024 * 1024:
                        return None, None
            with open(local_path, "wb") as f:
                f.write(data)
            return interactions.File(local_path), local_path
        except Exception as e:
            print(f"Debug - couldn't resolve image attachment: {e}")
            return None, None

    async def _download_video_attachment(self, video_url: str, notification_id: int) -> tuple[interactions.File | None, str | None]:
        """
        Download an MP4 to a temp directory and return an interactions.File attachment.

        Returns:
            (attachment, local_path)
        """
        if not video_url:
            return None, None

        await self._cleanup_old_video_attachments()
        os.makedirs(self._video_notif_dir, exist_ok=True)

        # Create a deterministic filename per notification to avoid collisions
        file_name = f"notif_{notification_id}.mp4"
        local_path = os.path.join(self._video_notif_dir, file_name)

        try:
            # Local-path mode (used by local storage backend)
            source_path = video_url
            if source_path.startswith("file://"):
                source_path = source_path[7:]
            if os.path.exists(source_path):
                size = os.path.getsize(source_path)
                if size <= 0 or size > self._video_notif_max_bytes:
                    return None, None
                await asyncio.to_thread(shutil.copyfile, source_path, local_path)
                return interactions.File(local_path), local_path

            # Remote URL mode (existing B2/CDN behavior)
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session_http:
                async with session_http.get(video_url) as resp:
                    if resp.status not in (200, 206):
                        return None, None

                    # Enforce a max size to avoid abuse/unexpected large downloads
                    content_length = resp.headers.get("Content-Length")
                    if content_length:
                        try:
                            if int(content_length) > self._video_notif_max_bytes:
                                return None, None
                        except Exception:
                            pass

                    size = 0
                    async with aiofiles.open(local_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > self._video_notif_max_bytes:
                                return None, None
                            await f.write(chunk)

            if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
                return None, None

            return interactions.File(local_path), local_path
        except Exception:
            return None, None

    def _should_defer_for_video(self, db_session, data: dict) -> tuple[bool, str]:
        """
        Decide whether we should defer sending this notification because it has an
        associated video that is not finished processing yet.

        Rules:
        - If no video_key is present: do not defer
        - If VideoUpload not found: do not defer (fallback to sending without video)
        - If VideoUpload status is pending/uploaded/processing: defer
        - If VideoUpload status is processed: do not defer
        - If VideoUpload status is failed: do not defer (avoid blocking forever)
        """
        try:
            video_key = data.get("video_key")
            if not video_key:
                return False, ""

            rec = (
                db_session.query(VideoUpload)
                .filter(VideoUpload.video_key == video_key)
                .first()
            )
            if not rec:
                return False, ""

            status = (rec.status or "").lower().strip()
            if status in ("pending", "uploaded", "processing"):
                return True, f"Waiting for video processing (status={status})"
            if status in ("processed", "failed"):
                return False, ""
            return False, ""
        except Exception:
            return False, ""

    async def _fetch_sendable_channel(self, channel_id):
        """Fetch a Discord channel and verify it can actually receive messages.

        Returns ``(channel, error)`` — exactly one of the two is None.

        interactions.py resolves channels the bot can see but that are not
        messageable (categories, forums, or unknown types returned as a bare
        ``BaseChannel``) to classes without a ``.send`` coroutine. Calling
        ``.send`` on those raised "'BaseChannel' object has no attribute
        'send'", which polluted the queue with what looked like crashes.
        A hasattr-based guard keeps this working across channel classes.
        """
        channel = await self.bot.fetch_channel(channel_id=channel_id)
        if channel is None:
            return None, f"Channel {channel_id} not found"
        if not callable(getattr(channel, "send", None)):
            return None, f"Configured channel {channel_id} is not a text channel"
        return channel, None

    #@interactions.Task.create(interactions.IntervalTrigger(seconds=5))
    async def start(self):
        """Start the notification service.

        Creates a long-lived background task bound to the current running loop
        that periodically processes pending notifications. If the task already
        exists and is running, this is a no-op.
        """
        # If the task is already running, do nothing
        if self._task is not None and not self._task.done():
            self.running = True
            app_logger.log(log_type="info", data="Notification service already running", app_name="notification_service", description="start")
            return

        # If a previous task finished, drop the reference
        self._task = None
        self.running = True

        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self.process_notifications_loop(), name="notification_service_loop")
        app_logger.log(log_type="info", data="Notification service started successfully", app_name="notification_service", description="start")
    
    async def stop(self):
        """Stop the notification service.

        Cancels and awaits the background task to ensure a clean shutdown.
        """
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

    def is_running(self) -> bool:
        """Return True if the background loop task is alive and running."""
        return self.running and self._task is not None and not self._task.done()
    
    def get_statistics(self) -> dict:
        """Get service statistics for monitoring"""
        return {
            "running": self.is_running(),
            "processed_count": self.processed_count,
            "last_processed_at": self.last_processed_at.isoformat() if self.last_processed_at else None,
            "sleep_interval": self.sleep_interval
        }
    
    async def get_pending_notifications_count(self) -> dict:
        """Get count of pending notifications by type for debugging"""
        try:
            from api.core import get_db_session
            with get_db_session() as db_session:
                pending_count = db_session.query(NotificationQueue).filter(
                    NotificationQueue.status == 'pending'
                ).count()
                
                processing_count = db_session.query(NotificationQueue).filter(
                    NotificationQueue.status == 'processing'
                ).count()
                
                failed_count = db_session.query(NotificationQueue).filter(
                    NotificationQueue.status == 'failed'
                ).count()
                
                # Get breakdown by notification type
                type_breakdown = {}
                for notification_type in ['drop', 'pb', 'ca', 'clog', 'pet', 'new_npc', 'new_item', 'name_change', 'new_player', 'user_upgrade', 'group_upgrade', 'monetary_contribution', 'nitro_boost', 'nitro_boost_summary', 'update_log', 'points_earned']:
                    count = db_session.query(NotificationQueue).filter(
                        NotificationQueue.status == 'pending',
                        NotificationQueue.notification_type == notification_type
                    ).count()
                    if count > 0:
                        type_breakdown[notification_type] = count
                
                return {
                    "pending": pending_count,
                    "processing": processing_count,
                    "failed": failed_count,
                    "type_breakdown": type_breakdown
                }
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error getting pending notifications count: {e}", app_name="notification_service", description="get_pending_notifications_count")
            return {"error": str(e)}
    
    def set_sleep_interval(self, interval: int):
        """Set the sleep interval between processing cycles (in seconds)"""
        if interval < 1:
            raise ValueError("Sleep interval must be at least 1 second")
        self.sleep_interval = interval
        app_logger.log(log_type="info", data=f"Notification service sleep interval set to {interval}s", app_name="notification_service", description="set_sleep_interval")
    
    async def force_process_notifications(self):
        """Force immediate processing of pending notifications without affecting the main loop.
        
        This method can be called externally to trigger immediate notification processing
        without interfering with the existing background loop. It's safe to call multiple
        times concurrently as it uses the same locking mechanism as the main loop.
        
        Returns:
            int: Number of notifications processed in this call
        """
        if not self.is_running():
            app_logger.log(log_type="warning", data="Force processing called but service is not running", app_name="notification_service", description="force_process_notifications")
            return 0
        
        try:
            async with self._processing_lock:
                processed_count = await self.process_pending_notifications()
                
                # Update statistics
                if processed_count > 0:
                    self.processed_count += processed_count
                    self.last_processed_at = datetime.now()
                    app_logger.log(log_type="info", data=f"Force processing completed: {processed_count} notifications processed", app_name="notification_service", description="force_process_notifications")
                
                return processed_count
                
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error in force processing: {e}", app_name="notification_service", description="force_process_notifications")
            return 0

    async def _maybe_prune_notification_queue(self):
        """Run the retention prune at most once per day; never raise."""
        now = datetime.now()
        if self._last_queue_prune_at is not None and (now - self._last_queue_prune_at) < timedelta(days=1):
            return
        self._last_queue_prune_at = now
        try:
            await self.prune_notification_queue()
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error pruning notification queue: {e}", app_name="notification_service", description="prune_notification_queue")

    async def prune_notification_queue(self, batch_size: int = 5000):
        """Delete old sent/failed notification_queue rows in small batches.

        Retention: sent rows are kept 7 days, failed rows 30 days. Deletes
        run as ``DELETE ... LIMIT batch_size`` in a loop (yielding between
        batches) so the table — which has grown past a million rows — is
        never locked for long.
        """
        from api.core import get_db_session
        totals = {"sent": 0, "failed": 0}
        for status, days in (("sent", 7), ("failed", 30)):
            cutoff = datetime.now() - timedelta(days=days)
            while True:
                with get_db_session() as db_session:
                    result = db_session.execute(
                        text(
                            "DELETE FROM notification_queue "
                            "WHERE status = :status AND created_at < :cutoff "
                            f"LIMIT {int(batch_size)}"
                        ),
                        {"status": status, "cutoff": cutoff},
                    )
                    db_session.commit()
                    deleted = result.rowcount or 0
                totals[status] += deleted
                if deleted < batch_size:
                    break
                # Yield between batches so notification sending isn't starved.
                await asyncio.sleep(0.25)
        if totals["sent"] or totals["failed"]:
            app_logger.log(
                log_type="info",
                data=f"Pruned notification_queue: {totals['sent']} sent rows (>7d), {totals['failed']} failed rows (>30d)",
                app_name="notification_service",
                description="prune_notification_queue",
            )
        return totals

    async def process_notifications_loop(self):
        """Main loop to process notifications.

        This loop is resilient to transient errors and will continue running
        until explicitly stopped via `stop()`. It handles task cancellation
        gracefully to integrate with application shutdown.
        """
        cleanup_counter = 0
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        app_logger.log(log_type="info", data=f"Notification service loop started with {self.sleep_interval}s interval", app_name="notification_service", description="process_notifications_loop")
        
        while self.running:
            try:
                async with self._processing_lock:
                    #app_logger.log(log_type="debug", data="Notification service processing cycle started", app_name="notification_service", description="process_notifications_loop")
                    processed_count = await self.process_pending_notifications()
                    
                    # Update statistics
                    if processed_count > 0:
                        self.processed_count += processed_count
                        self.last_processed_at = datetime.now()
                        #app_logger.log(log_type="info", data=f"Processed {processed_count} notifications in this cycle", app_name="notification_service", description="process_notifications_loop")
                    # else:
                    #     app_logger.log(log_type="debug", data="No notifications to process in this cycle", app_name="notification_service", description="process_notifications_loop")
                    
                    # Reset error counter on successful processing
                    consecutive_errors = 0
                    
                    # Clean up tracking dictionaries and stuck notifications every 1000 iterations
                    # Only run cleanup if we've actually processed some notifications
                    cleanup_counter += 1
                    if cleanup_counter >= 1000 and self.processed_count > 0:
                        app_logger.log(log_type="info", data="Starting cleanup cycle", app_name="notification_service", description="process_notifications_loop")
                        await self.cleanup_tracking_dicts()
                        await self.cleanup_stuck_notifications()
                        cleanup_counter = 0
                        app_logger.log(log_type="info", data=f"Cleanup completed. Total processed: {self.processed_count}", app_name="notification_service", description="process_notifications_loop")

                # Once-a-day retention prune: sent rows kept 7 days, failed 30.
                await self._maybe_prune_notification_queue()
            except asyncio.CancelledError:
                # Graceful shutdown
                app_logger.log(log_type="info", data="Notification service loop cancelled", app_name="notification_service", description="process_notifications_loop")
                break
            except Exception as e:
                consecutive_errors += 1
                app_logger.log(log_type="error", data=f"Error processing notifications (attempt {consecutive_errors}): {e}", app_name="notification_service", description="process_notifications_loop")
                
                # If we have too many consecutive errors, increase sleep time
                if consecutive_errors >= max_consecutive_errors:
                    app_logger.log(log_type="warning", data=f"Too many consecutive errors ({consecutive_errors}), increasing sleep time", app_name="notification_service", description="process_notifications_loop")
                    await asyncio.sleep(10)  # Longer sleep on repeated errors
                    consecutive_errors = 0  # Reset after longer sleep
                    continue  # Skip the normal sleep interval after error recovery
            
            # Normal sleep time between iterations - ensures regular processing
            await asyncio.sleep(self.sleep_interval)
    
    async def process_pending_notifications(self):
        """Process pending notifications with improved locking strategy"""
        processed_count = 0
        try:
            # Use a fresh session for this operation
            from api.core import get_db_session
            with get_db_session() as db_session:
                # First, get a small batch of pending notifications without locking
                # Include deferred notifications waiting on video, but only retry them
                # at a modest interval to avoid tight loops.
                retry_cutoff = datetime.now() - timedelta(seconds=15)
                notifications = (
                    db_session.query(NotificationQueue)
                    .filter(
                        (NotificationQueue.status == "pending")
                        | (
                            (NotificationQueue.status == "video_processing")
                            & (
                                (NotificationQueue.processed_at.is_(None))
                                | (NotificationQueue.processed_at < retry_cutoff)
                            )
                        )
                    )
                    .order_by(NotificationQueue.created_at.asc())
                    .limit(10)
                    .all()
                )  # Increased batch size for better efficiency
                
                og_length = len(notifications)
                if og_length == 0:
                    # No pending notifications found, return immediately
                    return processed_count
                    
                app_logger.log(log_type="info", data=f"Processing {og_length} pending notifications", app_name="notification_service", description="process_pending_notifications")
                
                for notification in notifications:
                    try:
                        # Use a more targeted locking approach - lock only the specific row
                        locked_notification = db_session.query(NotificationQueue).filter(
                            NotificationQueue.id == notification.id,
                            NotificationQueue.status.in_(["pending", "video_processing"])
                        ).with_for_update(skip_locked=True).first()
                        
                        # Skip if already locked by another process or no longer pending
                        if not locked_notification:
                            continue
                            
                        # Mark as processing immediately after acquiring lock
                        locked_notification.status = 'processing'
                        db_session.commit()
                        
                        # Process the notification with the same session
                        await self.process_notification_with_session(locked_notification, db_session)
                        processed_count += 1
                        
                    except Exception as e:
                        # Ensure we have a valid notification object for error handling
                        if 'locked_notification' in locals() and locked_notification:
                            locked_notification.status = 'failed'
                            locked_notification.error_message = str(e)
                            db_session.commit()
                        app_logger.log(log_type="error", data=f"Error processing notification {notification.id}: {e}", app_name="notification_service", description="process_pending_notifications")
            
            return processed_count
            
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error in process_pending_notifications: {e}", app_name="notification_service", description="process_pending_notifications")
            return processed_count

    async def process_notification_with_session(self, notification: NotificationQueue, db_session):
        """Process a single notification based on its type with a specific session"""
        try:
            app_logger.log(log_type="info", data=f"Processing notification {notification.id} of type '{notification.notification_type}'", app_name="notification_service", description="process_notification")
            
            data = json.loads(notification.data)
            notification_type = notification.notification_type

            # If this notification includes a video_key, delay sending until the
            # video processing pipeline has finished (so embeds can include the URL).
            should_defer, defer_reason = self._should_defer_for_video(db_session, data)
            if should_defer:
                notification.status = "video_processing"
                notification.error_message = defer_reason
                # Reuse processed_at as "last checked at" while deferred; once sent,
                # processed_at will be overwritten with the actual sent time.
                notification.processed_at = datetime.now()
                db_session.commit()
                return

            # If a video is available, expose it as video_url.
            # Do NOT overwrite image_url because many senders treat image_url as
            # a local-file-backed screenshot for attachments.
            video_url = self._maybe_get_video_url(db_session, data)
            if video_url:
                data["video_url"] = video_url
            
            # Check for duplicates before processing
            if not await self._is_not_sent_with_session(notification, data, db_session):
                app_logger.log(log_type="info", data=f"Notification {notification.id} was already sent, skipping", app_name="notification_service", description="process_notification")
                # Mark as sent since it was already processed
                notification.status = 'sent'
                notification.processed_at = datetime.now()
                db_session.commit()
                return
            
            # Process the notification based on its type
            # Pass the db_session to each method for consistent session handling
            if notification_type == 'drop':
                await self.send_drop_notification_with_session(notification, data, db_session)
            elif notification_type == 'pb':
                await self.send_pb_notification_with_session(notification, data, db_session)
            elif notification_type == 'ca':
                await self.send_ca_notification_with_session(notification, data, db_session)
            elif notification_type == 'clog':
                await self.send_clog_notification_with_session(notification, data, db_session)
            elif notification_type == 'pet':
                await self.send_pet_notification_with_session(notification, data, db_session)
            elif notification_type == 'level_up':
                await self.send_level_up_notification_with_session(notification, data, db_session)
            elif notification_type in ('xp_milestone', 'total_level_milestone'):
                await self.send_xp_milestone_notification_with_session(notification, data, db_session)
            elif notification_type == 'quest':
                await self.send_quest_notification_with_session(notification, data, db_session)
            elif notification_type == 'death':
                await self.send_death_notification_with_session(notification, data, db_session)
            elif notification_type == 'diary':
                await self.send_diary_notification_with_session(notification, data, db_session)
            elif notification_type == 'new_npc':
                await self.send_new_npc_notification_with_session(notification, data, db_session)
            elif notification_type == 'new_item':
                await self.send_new_item_notification_with_session(notification, data, db_session)
            elif notification_type == 'name_change':
                await self.send_name_change_notification_with_session(notification, data, db_session)
            elif notification_type == 'new_player':
                await self.send_new_player_notification_with_session(notification, data, db_session)
            elif notification_type == 'user_upgrade':
                await self.send_user_upgrade_notification_with_session(notification, data, db_session)
            elif notification_type == 'group_upgrade':
                await self.send_group_upgrade_notification_with_session(notification, data, db_session)
            elif notification_type == 'monetary_contribution':
                await self.send_contribution_notification_with_session(notification, data, db_session)
            elif notification_type == 'nitro_boost':
                await self.send_nitro_boost_notification_with_session(notification, data, db_session)
            elif notification_type == 'nitro_boost_summary':
                await self.send_nitro_boost_summary_with_session(notification, data, db_session)
            elif notification_type == 'update_log':
                await self.send_update_log_data_with_session(notification, data, db_session)
            elif notification_type == 'points_earned':
                await self.send_points_notification_with_session(notification, data, db_session)
            elif notification_type in SUBMISSION_DM_TYPES:
                await self.send_submission_dm_with_session(notification, data, db_session)
            elif notification_type in EVENT_NOTIFICATION_TYPES:
                await self.send_event_notification_with_session(notification, data, db_session)
            else:
                notification.status = 'failed'
                notification.error_message = f"Unknown notification type: {notification_type}"
                print(f"Notification type not found: '{notification_type}'")
                db_session.commit()
        except interactions.errors.Forbidden:
            group_id = data.get('group_id', None)
            if group_id:
                group = db_session.query(Group).filter(Group.group_id == group_id).first()
                if group:
                    guild_id = group.guild_id
                    authorized_users = await get_authorized_users(group_id)
                    for user in authorized_users:
                        try:
                            discord_user = await self.bot.fetch_user(user_id=user)
                            await discord_user.send(f"Hey, <@{discord_user}>!\n" + 
                            f"We just tried to process a `{notification_type}` submission for your group, however the <@{self.bot.user.id}> does not have permissions to send messages in the channel you configured...\n\n" + 
                            f"Please make sure the bot has **Manage Messages** and **Read Message History** permissions in the channel(s) you are using for notifications!")
                        except Exception as e:
                            print("Couldn't DM server admin about failed notification (bot permissions in server)...")
            print(f"Forbidden access error attempting to send a notification to {group_id}")
            # Mark as failed and commit
            notification.status = 'failed'
            notification.error_message = "Forbidden access error"
            db_session.commit()
        except Exception as e:
            app_logger.log(log_type="error", data=f"Error processing notification {notification.id}: {e}", app_name="notification_service", description="process_notification")
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_level_up_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a level up notification to Discord with session"""
        notification.status = 'processing'
        db_session.commit()
        try:
            group_id = notification.group_id
            player_id = notification.player_id

            # Dedicated levels channel, falling back to the loot channel (the
            # config editor documents this fallback).
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_levels'
            ).first()
            if not channel_id_config or not channel_id_config.config_value:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()

            if not channel_id_config or not channel_id_config.config_value:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return

            channel, channel_error = await self._fetch_sendable_channel(channel_id_config.config_value)
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return

            # Get embed template
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('level_up', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('level_up', 1)

            if not embed_template:
                notification.status = 'failed'
                notification.error_message = f"No embed template for group {group_id}"
                db_session.commit()
                return

            # Data
            player_name = data.get("player_name") or ""
            image_url = data.get("image_url") or ""
            skills_text = data.get("skills_text") or ""
            if not skills_text:
                # Backwards compat with older payloads (single skill)
                sn = data.get("skill_name") or ""
                nl = data.get("new_level")
                lg = data.get("levels_gained")
                if sn and nl is not None:
                    if lg is not None:
                        skills_text = f"{sn} {nl} (+{lg})"
                    else:
                        skills_text = f"{sn} {nl}"

            formatted_name = get_formatted_name(player_name, group_id, db_session)

            replacements = {
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                "{skill_name}": str(data.get("skill_name") or data.get("skills_names") or ""),
                "{skills_names}": str(data.get("skills_names") or ""),
                "{skills_text}": str(skills_text or ""),
                "{new_level}": str(data.get("new_level") or ""),
                "{levels_gained}": str(data.get("levels_gained") or ""),
                "{xp_total}": str(data.get("xp_total") or ""),
                "{total_level}": str(data.get("total_level") or ""),
                "{total_xp}": str(data.get("total_xp") or ""),
                "{combat_level}": str(data.get("combat_level") or ""),
                "{image_url}": image_url,
                "{video_url}": "",
                "{video_link}": "",
            }
            replacements.update(self._plugin_version_placeholder_map(data))

            embed = replace_placeholders(embed_template, replacements)
            if group_id == 2:
                embed = await self.remove_group_field(embed)

            content = f"{formatted_name} levelled-up:"

            if image_url:
                try:
                    local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                    if os.path.exists(local_path):
                        attachment = interactions.File(local_path)
                        await channel.send(content, embed=embed, files=attachment)
                    else:
                        await channel.send(content, embed=embed)
                except Exception:
                    await channel.send(content, embed=embed)
            else:
                await channel.send(content, embed=embed)

            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_xp_milestone_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a post-99 XP milestone or total-level milestone notification.

        Both types reuse the group's level_up embed template so no new embed
        rows are required; only the content line differs.
        """
        notification.status = 'processing'
        db_session.commit()
        try:
            group_id = notification.group_id
            player_id = notification.player_id

            # Dedicated levels channel, falling back to the loot channel.
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_levels'
            ).first()
            if not channel_id_config or not channel_id_config.config_value:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()

            if not channel_id_config or not channel_id_config.config_value:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return

            channel, channel_error = await self._fetch_sendable_channel(channel_id_config.config_value)
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return

            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('level_up', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('level_up', 1)

            if not embed_template:
                notification.status = 'failed'
                notification.error_message = f"No embed template for group {group_id}"
                db_session.commit()
                return

            player_name = data.get("player_name") or ""
            image_url = data.get("image_url") or ""
            skills_text = data.get("skills_text") or ""

            formatted_name = get_formatted_name(player_name, group_id, db_session)

            replacements = {
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                "{skill_name}": str(data.get("skill_name") or data.get("skills_names") or ""),
                "{skills_names}": str(data.get("skills_names") or ""),
                "{skills_text}": str(skills_text or ""),
                "{new_level}": str(data.get("new_level") or ""),
                "{levels_gained}": str(data.get("levels_gained") or ""),
                "{xp_total}": str(data.get("xp_total") or ""),
                "{milestone_xp}": str(data.get("milestone_xp") or ""),
                "{total_level}": str(data.get("total_level") or ""),
                "{total_xp}": str(data.get("total_xp") or ""),
                "{combat_level}": str(data.get("combat_level") or ""),
                "{image_url}": image_url,
                "{video_url}": "",
                "{video_link}": "",
            }
            replacements.update(self._plugin_version_placeholder_map(data))

            embed = replace_placeholders(embed_template, replacements)
            if group_id == 2:
                embed = await self.remove_group_field(embed)

            if notification.notification_type == 'total_level_milestone':
                milestone_label = str(data.get("total_level") or "")
                content = (
                    f"{formatted_name} reached total level {milestone_label}!"
                    if milestone_label
                    else f"{formatted_name} reached a total level milestone!"
                )
            else:
                content = f"{formatted_name} reached an XP milestone: {skills_text}"

            if image_url:
                try:
                    local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                    if os.path.exists(local_path):
                        attachment = interactions.File(local_path)
                        await channel.send(content, embed=embed, files=attachment)
                    else:
                        await channel.send(content, embed=embed)
                except Exception:
                    await channel.send(content, embed=embed)
            else:
                await channel.send(content, embed=embed)

            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_quest_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a quest completion notification to Discord with session"""
        notification.status = 'processing'
        db_session.commit()
        try:
            group_id = notification.group_id
            player_id = notification.player_id

            # Channel config (inferred). Fallback to loot channel.
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_quests'
            ).first()
            if not channel_id_config or not channel_id_config.config_value:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
            if not channel_id_config or not channel_id_config.config_value:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return

            channel, channel_error = await self._fetch_sendable_channel(channel_id_config.config_value)
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return

            # Embed template
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('quest', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('quest', 1)

            player_name = data.get("player_name") or ""
            quest_name = data.get("quest_name") or ""
            image_url = data.get("image_url") or ""

            video_url = self._maybe_get_video_url(db_session, data)

            formatted_name = get_formatted_name(player_name, group_id, db_session)
            if embed_template:
                replacements = {
                    "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                    "{quest_name}": str(quest_name),
                    "{quests_completed}": str(data.get("quests_completed") or ""),
                    "{total_quests}": str(data.get("total_quests") or ""),
                    "{completion_percentage}": str(data.get("completion_percentage") or ""),
                    "{quest_points}": str(data.get("quest_points") or ""),
                    "{total_quest_points}": str(data.get("total_quest_points") or ""),
                    "{qp_percentage}": str(data.get("qp_percentage") or ""),
                    "{timestamp}": str(data.get("timestamp") or ""),
                    "{video_url}": video_url or "",
                    "{video_link}": f"[Video]({video_url})" if video_url else "",
                    # Prefer video for display; keep screenshot in data["image_url"] for attachments
                    "{image_url}": video_url or image_url or "",
                }
                replacements.update(self._plugin_version_placeholder_map(data))

                embed = replace_placeholders(embed_template, replacements)
                if group_id == 2:
                    embed = await self.remove_group_field(embed)
            else:
                embed = self._build_default_quest_embed(
                    data=data,
                    player_name=player_name,
                    player_id=player_id,
                    video_url=video_url,
                )

            content = f"{formatted_name} completed a quest!"

            # Prefer attaching MP4 if available; otherwise attach screenshot if present.
            video_attachment, video_local_path = (None, None)
            if video_url:
                video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)

            try:
                if video_attachment:
                    await channel.send(content, embed=embed, files=video_attachment)
                elif image_url:
                    try:
                        local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                        if os.path.exists(local_path):
                            attachment = interactions.File(local_path)
                            await channel.send(content, embed=embed, files=attachment)
                        else:
                            await channel.send(content, embed=embed)
                    except Exception:
                        await channel.send(content, embed=embed)
                else:
                    await channel.send(content, embed=embed)
            finally:
                if video_local_path:
                    try:
                        os.remove(video_local_path)
                    except Exception:
                        pass

            await self._cleanup_processed_local_video_after_send(db_session, data)
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()

        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_death_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a player death notification to Discord with session"""
        notification.status = 'processing'
        db_session.commit()
        try:
            group_id = notification.group_id
            player_id = notification.player_id

            # Channel config. Fallback to loot channel.
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_deaths'
            ).first()
            if not channel_id_config or not channel_id_config.config_value:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
            if not channel_id_config or not channel_id_config.config_value:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return

            channel, channel_error = await self._fetch_sendable_channel(channel_id_config.config_value)
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return

            # Embed template (optional; default embed is used when none exists)
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('death', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('death', 1)

            player_name = data.get("player_name") or ""
            source = data.get("source") or ""
            location = data.get("location") or ""
            image_url = data.get("image_url") or ""

            video_url = self._maybe_get_video_url(db_session, data)

            formatted_name = get_formatted_name(player_name, group_id, db_session)
            if embed_template:
                replacements = {
                    "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                    "{source}": str(source),
                    "{killer}": str(source),
                    "{location}": str(location),
                    "{region_id}": str(data.get("region_id") or ""),
                    "{timestamp}": str(data.get("timestamp") or ""),
                    "{video_url}": video_url or "",
                    "{video_link}": f"[Video]({video_url})" if video_url else "",
                    # Prefer video for display; keep screenshot in data["image_url"] for attachments
                    "{image_url}": video_url or image_url or "",
                }
                replacements.update(self._plugin_version_placeholder_map(data))

                embed = replace_placeholders(embed_template, replacements)
                if group_id == 2:
                    embed = await self.remove_group_field(embed)
            else:
                embed = self._build_default_death_embed(
                    data=data,
                    player_name=player_name,
                    player_id=player_id,
                    video_url=video_url,
                )

            content = f"{formatted_name} has died!"

            # Prefer attaching MP4 if available; otherwise attach screenshot if present.
            video_attachment, video_local_path = (None, None)
            if video_url:
                video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)

            try:
                if video_attachment:
                    await channel.send(content, embed=embed, files=video_attachment)
                elif image_url:
                    try:
                        local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                        if os.path.exists(local_path):
                            attachment = interactions.File(local_path)
                            await channel.send(content, embed=embed, files=attachment)
                        else:
                            await channel.send(content, embed=embed)
                    except Exception:
                        await channel.send(content, embed=embed)
                else:
                    await channel.send(content, embed=embed)
            finally:
                if video_local_path:
                    try:
                        os.remove(video_local_path)
                    except Exception:
                        pass

            await self._cleanup_processed_local_video_after_send(db_session, data)
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()

        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_diary_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send an achievement diary completion notification to Discord with session"""
        notification.status = 'processing'
        db_session.commit()
        try:
            group_id = notification.group_id
            player_id = notification.player_id

            # Channel config. Fallback to loot channel.
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_diaries'
            ).first()
            if not channel_id_config or not channel_id_config.config_value:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
            if not channel_id_config or not channel_id_config.config_value:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return

            channel, channel_error = await self._fetch_sendable_channel(channel_id_config.config_value)
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return

            # Embed template (optional; default embed is used when none exists)
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('diary', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('diary', 1)

            player_name = data.get("player_name") or ""
            diary_name = data.get("diary_name") or ""
            diary_tier = data.get("diary_tier") or ""
            image_url = data.get("image_url") or ""

            video_url = self._maybe_get_video_url(db_session, data)

            formatted_name = get_formatted_name(player_name, group_id, db_session)
            if embed_template:
                replacements = {
                    "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                    "{diary_name}": str(diary_name),
                    "{diary_tier}": str(diary_tier),
                    "{timestamp}": str(data.get("timestamp") or ""),
                    "{video_url}": video_url or "",
                    "{video_link}": f"[Video]({video_url})" if video_url else "",
                    # Prefer video for display; keep screenshot in data["image_url"] for attachments
                    "{image_url}": video_url or image_url or "",
                }
                replacements.update(self._plugin_version_placeholder_map(data))

                embed = replace_placeholders(embed_template, replacements)
                if group_id == 2:
                    embed = await self.remove_group_field(embed)
            else:
                embed = self._build_default_diary_embed(
                    data=data,
                    player_name=player_name,
                    player_id=player_id,
                    video_url=video_url,
                )

            content = f"{formatted_name} completed an achievement diary!"

            # Prefer attaching MP4 if available; otherwise attach screenshot if present.
            video_attachment, video_local_path = (None, None)
            if video_url:
                video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)

            try:
                if video_attachment:
                    await channel.send(content, embed=embed, files=video_attachment)
                elif image_url:
                    try:
                        local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                        if os.path.exists(local_path):
                            attachment = interactions.File(local_path)
                            await channel.send(content, embed=embed, files=attachment)
                        else:
                            await channel.send(content, embed=embed)
                    except Exception:
                        await channel.send(content, embed=embed)
                else:
                    await channel.send(content, embed=embed)
            finally:
                if video_local_path:
                    try:
                        os.remove(video_local_path)
                    except Exception:
                        pass

            await self._cleanup_processed_local_video_after_send(db_session, data)
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()

        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_update_log_data_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a update log data notification to Discord with session"""
        notification.status = 'processing'
        db_session.commit()
        try:
            channel_id = 1210765287591256084
            channel, channel_error = await self._fetch_sendable_channel(channel_id)
            if channel:
                updates = data.get('updates')
                updates = ["- " + update + "\n" for update in updates]
                text = f"### A new update log has been published:\n\n"
                text += f"".join(updates)
                await channel.send(text)
                notification.status = 'sent'
                notification.processed_at = datetime.now()
                db_session.commit()
            else:
                notification.status = 'failed'
                notification.error_message = channel_error or "Channel not found"
                db_session.commit()
                return
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            app_logger.log(log_type="error", data=f"Error sending update log data notification: {e}", app_name="notification_service", description="send_update_log_data")
            raise

    # ------------------------------------------------------------------ #
    # Events (Task 19) — event_started / event_ended / event_completion /
    # event_line / event_blackout / event_lead_change /
    # event_pending, produced by services/event_engine.py (+ Tasks 20/21).
    # ------------------------------------------------------------------ #
    def _resolve_task_icon_url(self, db_session, task_id) -> str | None:
        """Best icon URL for an event task's target — the item/NPC/skill a
        completion or progress message is about.

        Reuses the site's task-tile derivation (web_api/task_tiles.py) so
        Discord and the website pick the same icon, resolves the tile's names
        to game ids in two small bulk queries (mirroring ``_attach_task_tiles``),
        and returns the first icon whose asset actually exists on disk — so a
        task with no resolvable icon (custom/ehp, unknown item) simply gets no
        thumbnail. Never raises: icon lookup must not break a notification.
        """
        if not task_id:
            return None
        try:
            from sqlalchemy import func
            from db import ItemList, NpcList
            from db.models import EventTask
            from web_api.task_tiles import (
                build_tile,
                icon_asset_path,
                spec_names,
                tile_spec,
            )

            task = db_session.query(EventTask).filter(EventTask.id == task_id).first()
            if not task:
                return None
            spec = tile_spec({
                "id": task.id, "type": task.type, "label": task.label,
                "target": task.target, "target_value": task.target_value,
                "config": task.config,
            })
            item_names, npc_names = spec_names(spec)
            item_ids: dict = {}
            if item_names:
                for iid, name in (
                    db_session.query(func.min(ItemList.item_id), ItemList.item_name)
                    .filter(ItemList.item_name.in_(item_names), ItemList.noted.is_(False))
                    .group_by(ItemList.item_name)
                    .all()
                ):
                    item_ids[" ".join(name.strip().lower().split())] = iid
            npc_ids: dict = {}
            if npc_names:
                for nid, name in (
                    db_session.query(func.min(NpcList.npc_id), NpcList.npc_name)
                    .filter(NpcList.npc_name.in_(npc_names))
                    .group_by(NpcList.npc_name)
                    .all()
                ):
                    npc_ids[" ".join(name.strip().lower().split())] = nid
            tile = build_tile(spec, item_ids, npc_ids)
            for icon in tile.get("icons") or []:
                rel = icon_asset_path(icon)
                if rel and os.path.exists(os.path.join(STATIC_IMG_DIR, rel)):
                    return f"{IMG_BASE}/{rel}"
        except Exception as e:
            app_logger.log(log_type="warning",
                           data=f"task icon resolution failed for task {task_id}: {e}",
                           app_name="notification_service",
                           description="resolve_task_icon")
        return None

    def _resolve_item_icon_url(self, db_session, item_name) -> str | None:
        """Icon URL for a specific received item (``EventCompletion.matched_target``)
        — the item the completing drop delivered. Resolves the item name to its
        game id and returns ``itemdb/{id}.png`` when that asset exists on disk,
        else None. Used as the completion message's section thumbnail so the
        card shows the item that was received (the proof screenshot rides below
        as the full image). Never raises."""
        if not item_name:
            return None
        try:
            from sqlalchemy import func
            from db import ItemList
            from web_api.task_tiles import icon_asset_path

            key = " ".join(str(item_name).strip().lower().split())
            iid = (db_session.query(func.min(ItemList.item_id))
                   .filter(ItemList.item_name == item_name, ItemList.noted.is_(False))
                   .scalar())
            if iid is None:
                return None
            rel = icon_asset_path({"type": "item", "id": iid})
            if rel and os.path.exists(os.path.join(STATIC_IMG_DIR, rel)):
                return f"{IMG_BASE}/{rel}"
        except Exception as e:
            app_logger.log(log_type="warning",
                           data=f"item icon resolution failed for {item_name!r}: {e}",
                           app_name="notification_service",
                           description="resolve_item_icon")
        return None

    async def send_event_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send one event notification to its per-event Discord destination.

        Channel resolution reads ``web_event_channels`` by (event_id, kind);
        completions/leaderboard/admin fall back to announcements; with nothing
        configured the row is marked processed and skipped silently (spec) —
        no error spam for events that simply don't use Discord.
        """
        from db.models import Event, EventTeam

        image_attachment, image_temp_path, image_ref = None, None, None
        try:
            notification_type = notification.notification_type
            event_id = data.get('event_id')
            event = db_session.query(Event).filter(Event.id == event_id).first() if event_id else None
            if not event:
                notification.status = 'failed'
                notification.error_message = f"Unknown event {event_id}"
                db_session.commit()
                return

            # Destination resolution. Two shapes (web48a):
            # - default: ONE channel set + the event's own verbosity config.
            # - per-group clan-vs-clan (Event.per_group_discord): every
            #   accepted clan gets its own channels + verbosity; destinations
            #   dedupe by resolved channel id so clans falling back to the
            #   shared set never double-post. Role pings only fire on the
            #   host clan's destination (role ids are guild-specific).
            from services.event_notifications import (
                load_group_destinations,
                per_group_discord_enabled,
            )

            destinations = []  # [{channel_id, ping}]
            if per_group_discord_enabled(event):
                seen_channels = set()
                for dest in load_group_destinations(db_session, event):
                    if not should_send_event_message(dest["message_config"], notification_type):
                        continue
                    cid = resolve_event_channel(dest["channels"], notification_type)
                    if not cid or cid in seen_channels:
                        continue
                    seen_channels.add(cid)
                    destinations.append({
                        "channel_id": cid,
                        "ping": dest["group_id"] == event.group_id,
                    })
                skip_reason = ('skipped: no participating clan wants this message'
                               if not destinations else None)
            else:
                # Verbosity re-check at send time (the engine already gates at
                # enqueue; this covers rows queued before a config change).
                message_config = effective_message_config(getattr(event, "message_config", None))
                if not should_send_event_message(message_config, notification_type):
                    notification.status = 'sent'
                    notification.error_message = 'skipped: muted by event message config'
                    notification.processed_at = datetime.now()
                    db_session.commit()
                    return
                channels = load_event_channels(db_session, event.id)
                channel_id = resolve_event_channel(channels, notification_type)
                if channel_id:
                    destinations.append({"channel_id": channel_id, "ping": True})
                skip_reason = ('skipped: no event channel configured'
                               if not destinations else None)
            if skip_reason:
                # Nothing configured for this kind (or at all) — processed, skipped.
                notification.status = 'sent'
                notification.error_message = skip_reason
                notification.processed_at = datetime.now()
                db_session.commit()
                return

            # --- enrichment the embed needs but the queue payload may lack ---
            data = dict(data)
            data.setdefault('event_name', event.name)
            # Start/end (unix secs, the _ts convention) so every event message
            # can carry the universal footer — not just event_started/signup,
            # whose payloads already include them.
            if event.starts_at and not data.get('starts_at'):
                data['starts_at'] = int(event.starts_at.timestamp())
            if event.ends_at and not data.get('ends_at'):
                data['ends_at'] = int(event.ends_at.timestamp())
            team_id = data.get('team_id')
            if team_id and not data.get('team_name'):
                team = db_session.query(EventTeam).filter(EventTeam.id == team_id).first()
                if team:
                    data['team_name'] = team.name

            # Task-tile icon: completion/progress messages show the item/NPC/skill
            # the task is about (same icon the website's task tiles use). On
            # completion a real proof screenshot still wins; the icon is the
            # fallback so a completion is never image-less.
            if notification_type in ('event_completion', 'event_task_progress') and data.get('task_id'):
                task_icon = self._resolve_task_icon_url(db_session, data.get('task_id'))
                if task_icon:
                    data['task_icon'] = task_icon
                if notification_type == 'event_completion':
                    # Section thumbnail = the item that was received (falls back
                    # to the task tile when there's no resolvable item). The
                    # proof screenshot is NOT the thumbnail — it rides below as
                    # the full image (image_ref), so it isn't shown twice.
                    completion_icon = (
                        self._resolve_item_icon_url(db_session, data.get('received_item'))
                        or task_icon
                    )
                    if completion_icon:
                        data['completion_icon'] = completion_icon

            # Real proof screenshot: attach it as a full Discord image, the
            # same treatment submission-processing notifications (drop, pb,
            # ...) get — on top of the small task-tile thumbnail above, which
            # stays icon-only. Progress messages now carry the proof of the
            # ledger row that drove them (services.event_engine enrichment).
            if notification_type in ('event_completion', 'event_task_progress'):
                proof_url = data.get('proof_url')
                if proof_url:
                    image_attachment, image_temp_path = await self._resolve_image_attachment(
                        proof_url, notification.id)
                    if image_attachment is not None:
                        image_ref = f"attachment://{image_attachment.file_name}"

            standings = None
            if notification_type in ('event_lead_change', 'event_ended'):
                limit = 3 if notification_type == 'event_lead_change' else 5
                rows = (db_session.query(EventTeam)
                        .filter(EventTeam.event_id == event.id)
                        .order_by(EventTeam.score.desc(), EventTeam.id.asc())
                        .limit(limit).all())
                standings = [{"name": t.name, "score": int(t.score or 0)} for t in rows]

            if notification_type == 'event_pending' and not data.get('review_url'):
                # Deep-link to the Review tab (the group event manager page);
                # global events review from the public event page.
                if event.group_id:
                    data['review_url'] = f"https://www.droptracker.io/groups/{event.group_id}/events/{event.id}"
                else:
                    data['review_url'] = event_url(event.id)

            # The interactive "Sign up" prompt carries a button that opens the
            # in-Discord signup flow (services/event_signup_discord.py).
            extra_rows = []
            if notification_type == "event_signup_prompt":
                try:
                    extra_rows.append(interactions.ActionRow(interactions.Button(
                        style=interactions.ButtonStyle.PRIMARY,
                        label="Sign up",
                        emoji="\U0001F4DD",
                        custom_id=f"evtsignup:{event.id}",
                    )))
                except Exception:
                    extra_rows = []  # never let a component error drop the post

            # Configured role pings (web_events.ping_config). Components V2
            # forbids content= alongside components, so mentions render as
            # the container's first text display — they still notify under
            # allowed_mentions.
            configured_ping = ping_content(
                event_ping_role_ids(getattr(event, "ping_config", None), notification_type)
            )

            async def _send_to(channel, ping_text):
                """One destination: Components-V2 layout, embed fallback."""
                allowed = interactions.AllowedMentions(parse=["roles"]) if ping_text else None
                # Preferred path: the group's Components-V2 layout
                # (services/event_message_layouts.py — group row falls back to
                # the template group's seeded defaults). Any render failure
                # falls back to the legacy embed so a bad layout can't silence
                # an event.
                try:
                    from services.activity_launch import channel_supports_launch
                    from services.event_message_layouts import (
                        notification_context,
                        render_event_components,
                    )

                    components = render_event_components(
                        db_session, event.group_id, notification_type,
                        notification_context(notification_type, data),
                        standings=standings, ping_text=ping_text,
                        extra_rows=extra_rows,
                        # Threads/announcement channels can't launch the
                        # Activity — render the URL button instead of a dead
                        # launch button.
                        allow_launch=channel_supports_launch(channel),
                        image_ref=image_ref,
                    )
                    send_kwargs = {"files": image_attachment} if image_attachment else {}
                    if allowed:
                        await channel.send(components=components,
                                           allowed_mentions=allowed, **send_kwargs)
                    else:
                        await channel.send(components=components, **send_kwargs)
                except interactions.errors.Forbidden:
                    raise
                except Exception as render_error:
                    app_logger.log(log_type="error",
                                   data=f"Component render failed for {notification_type} "
                                        f"(event {event.id}): {render_error} — falling back to embed",
                                   app_name="notification_service",
                                   description="send_event_notification")
                    from utils.embeds import build_event_embed
                    embed = build_event_embed(notification_type, data, standings=standings,
                                              image_attachment_ref=image_ref)
                    send_kwargs = {"components": extra_rows} if extra_rows else {}
                    if image_attachment:
                        send_kwargs["files"] = image_attachment
                    if ping_text:
                        await channel.send(content=ping_text, embed=embed,
                                           allowed_mentions=allowed, **send_kwargs)
                    else:
                        await channel.send(embed=embed, **send_kwargs)

            # Deliver to every destination; per-group events keep going when
            # one clan's channel is broken (their misconfig must not silence
            # the other clans). Single-destination events keep the original
            # semantics: failure marks the row failed / Forbidden propagates.
            sent_count, dest_errors = 0, []
            for dest in destinations:
                channel, channel_error = await self._fetch_sendable_channel(dest["channel_id"])
                if channel is None:
                    dest_errors.append(
                        channel_error or f"Channel {dest['channel_id']} not found for event {event.id}")
                    continue
                try:
                    await _send_to(channel, configured_ping if dest["ping"] else None)
                    sent_count += 1
                except interactions.errors.Forbidden:
                    if len(destinations) == 1:
                        raise
                    dest_errors.append(f"forbidden: channel {dest['channel_id']}")
                except Exception as send_error:
                    if len(destinations) == 1:
                        raise
                    dest_errors.append(f"channel {dest['channel_id']}: {send_error}")

            if sent_count == 0:
                notification.status = 'failed'
                notification.error_message = "; ".join(dest_errors)[:500] or "no sendable channel"
                db_session.commit()
                return

            notification.status = 'sent'
            notification.error_message = ("; ".join(dest_errors)[:500] or None) if dest_errors else None
            notification.processed_at = datetime.now()
            db_session.commit()

            # Keep the live standings board hot on score changes. The
            # notification is already committed 'sent' — board upkeep must
            # never rewrite that, so even the import stays guarded.
            try:
                from services.event_board import refresh_after_notification
                await refresh_after_notification(self.bot, db_session, event, notification_type)
            except Exception:
                pass  # refresh_event_board logs its own errors
        except interactions.errors.Forbidden:
            raise  # process_notification_with_session handles missing perms
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            app_logger.log(log_type="error",
                           data=f"Error sending {notification.notification_type} notification: {e}",
                           app_name="notification_service",
                           description="send_event_notification")
            raise
        finally:
            if image_temp_path:
                try:
                    os.remove(image_temp_path)
                except Exception:
                    pass

    async def send_points_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a points earned notification to Discord with session"""
        try:
            scope_earned = data.get('scope_earned')
            if not scope_earned:
                return
            match scope_earned:
                case 'player':
                    user_id = data.get('user_id')
                    user = db_session.query(User).filter(User.user_id == user_id).first()
                case 'group':
                    group_id = data.get('group_id')
                    group = db_session.query(Group).filter(Group.group_id == group_id).first()
            if not user and not group:
                return
            if user:
                user_name = user.username
            else:
                user_name = group.group_name
            amount_earned = data.get('amount_earned')
            source = data.get('source')
            new_total = data.get('current_total')
            comment = data.get('comment')
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            app_logger.log(log_type="error", data=f"Error sending points earned notification: {e}", app_name="notification_service", description="send_points_notification")
            raise

    async def send_group_upgrade_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a group upgrade notification to Discord with session"""
        try:
            group_id = data.get('group_id')
            group = db_session.query(Group).filter(Group.group_id == group_id).first()
            user_id = data.get('dt_id')
            user = db_session.query(User).filter(User.user_id == user_id).first()
            status = data.get('status') 
            if user.players:
                players = [player for player in user.players]
                player_name = players[0].player_name
            else:
                player_name = None
            global_embed = None
            group_embed = None
            global_channel = None
            channel = None
            if group and user:
                bot: interactions.Client = self.bot
                guild_id = group.guild_id
                guild = await bot.fetch_guild(guild_id)
                if guild:
                    channel = guild.public_updates_channel
                global_channel = await bot.fetch_channel(1373331322709479485)
                if channel:
                    match status:
                        case 'added':
                            group_embed = interactions.Embed(
                                title=f"<:supporter:1263827303712948304> Your group has been upgraded!",
                                description=f"<@{user.discord_id}> has upgraded {group.group_name} to unlock premium features, such as customizable embeds!",
                                color="#00f0f0"
                            )
                            group_embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            group_embed.add_field(
                                name="Thank you for your support!",
                                value="Developing and maintaining a project like this takes lots of time and effort. We're extremely grateful for your continued support!"
                            )
                            group_embed.set_footer(global_footer)
                            global_embed = interactions.Embed(
                                title=f"<:supporter:1263827303712948304> `{user.username}` just upgraded {group.group_name}!",
                                description=f"{player_name if player_name else f'<@{user.discord_id}>'} just used their [account upgrade benefits](https://www.droptracker.io/groups/upgrades) to unlock premium features for [{group.group_name}](https://www.droptracker.io/groups/{group.group_name}.{group.group_id}/view)",
                                color="#00f0f0"
                            )
                            global_embed.add_field(
                                name="Thank you for your support!",
                                value="Contributions like this keep us motivated to continue maintaining the project."
                            )
                            global_embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            global_embed.set_footer(global_footer)
                            global_guild = await bot.fetch_guild(1172737525069135962)
                            guild_member = await global_guild.fetch_member(user.discord_id)
                            if guild_member:
                                premium_role = global_guild.get_role(role_id=1210765189625151592)
                                await guild_member.add_role(role=premium_role)
                        case 'expired':
                            group_embed = interactions.Embed(
                                title=f"<:supporter:1263827303712948304> Your group has been downgraded!",
                                description=f"Your group upgrade has now expired.",
                                color="#f00000"
                            )
                            group_embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            group_embed.add_field(
                                name="Thank you for your support!",
                                value="Developing and maintaining a project like this takes lots of time and effort. We're extremely grateful for any support you provided."
                            )
                            group_embed.set_footer(global_footer)
                            global_guild = await bot.fetch_guild(1172737525069135962)
                            guild_member = await global_guild.fetch_member(user.discord_id)
                            if guild_member:
                                premium_role = global_guild.get_role(role_id=1210765189625151592)
                                if premium_role in guild_member.roles:
                                    await guild_member.remove_role(role=premium_role)
                    if channel and group_embed:
                        try:
                            await channel.send(embed=group_embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                        except Exception as e:
                            if group.configurations:
                                for config in group.configurations:
                                    if config.config_key == 'authed_users':
                                        authed_users = config.config_value
                                        authed_users = authed_users.replace('[','').replace(']','').replace('"','').replace(' ', '').split(',')
                                        for user_id in authed_users:
                                            user_id = int(user_id)
                                            try:
                                                authed_user = await bot.fetch_user(user_id)
                                                if authed_user:
                                                    if status == 'expired':
                                                        group_embed.add_field(
                                                        name=f"Original Supporter:",
                                                        value=f"<@{user.discord_id}>",
                                                        inline=False
                                                    )
                                                    if user_id in self.notified_users:
                                                        ## Don't notify the same user twice in quick succession.
                                                        return
                                                    self.notified_users.append(user_id)
                                                    await authed_user.send(embed=group_embed)
                                                    await asyncio.sleep(0.2)
                                            except Exception as e:
                                                app_logger.log(log_type="error", data=f"Error sending group embed to authed user {user_id}: {e}", app_name="notification_service", description="send_group_upgrade_notification")
                            app_logger.log(log_type="error", data=f"Error sending group embed: {e}", app_name="notification_service", description="send_group_upgrade_notification")
                    else:
                        app_logger.log(log_type="error", data=f"Channel or group embed not found", app_name="notification_service", description="send_group_upgrade_notification")
                    if global_channel and global_embed:
                        try:
                            await global_channel.send(embed=global_embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                            db_session.commit()
                        except Exception as e:
                            app_logger.log(log_type="error", data=f"Error sending global embed: {e}", app_name="notification_service", description="send_group_upgrade_notification")
                    else:
                        app_logger.log(log_type="error", data=f"Global channel or global embed not found", app_name="notification_service", description="send_group_upgrade_notification")
                    
                    db_session.commit()
                else:
                    notification.status = 'failed'
                    notification.error_message = f"Channel not found"
                    db_session.commit()
            else:
                notification.status = 'failed'
                notification.error_message = f"Group not found"
                db_session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_user_upgrade_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a user upgrade notification to Discord with session"""
        try:
            user_id = data.get('dt_id')
            status = data.get('status')
            db_user = db_session.query(User).filter(User.user_id == user_id).first()
            if user_id in self.notified_users:
                ## Don't notify the same user twice in quick succession.
                return
            self.notified_users.append(user_id)
            if db_user:
                bot: interactions.Client = self.bot
                user = await bot.fetch_user(db_user.discord_id)
                if user:
                    match status:
                        case 'added':
                            embed = interactions.Embed(
                                title="<a:droptracker:1346787143778963497> Thank you for your support!",
                                description=f"Your account upgrade has been successfully processed.",
                                color="#00f0f0"
                            )
                            embed.add_field(
                                name="What's next?",
                                value="You can now [select a group](https://www.droptracker.io/account/premium)" + 
                                " to use your premium features on.\n\n" + 
                                "If you have any questions, [feel free to reach out in our Discord](https://www.droptracker.io/discord)"
                            )
                            embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            embed.set_footer(global_footer)
                            await user.send(embed=embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                            db_session.commit()
                            return
                        case 'expired':
                            embed = interactions.Embed(
                                title="We're sorry to see you go!",
                                description=f"Your account upgrade has expired.\n" +
                                "Please consider [re-upgrading your account](https://www.droptracker.io/groups/upgrades) to continue supporting the project," + 
                                " and to retain access to your group's premium features.",
                                color="#f00000"
                            )
                            embed.set_thumbnail("https://www.droptracker.io/img/droptracker-small.gif")
                            
                            embed.set_footer(global_footer)
                            await user.send(embed=embed)
                            notification.status = 'sent'
                            notification.processed_at = datetime.now()
                            db_session.commit()
                            return
            else:
                notification.status = 'failed'
                notification.error_message = f"User not found"
                db_session.commit()
                return
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            app_logger.log(log_type="error", data=f"Error sending user upgrade notification: {e}", app_name="notification_service", description="send_user_upgrade_notification")
            raise

    async def send_contribution_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Announce a monetary contribution (first payment of a subscription).

        Three surfaces, each best-effort:
          1. the global supporters channel (public thank-you),
          2. a thank-you DM to the contributor,
          3. for group contributions, the group's own Discord.
        """
        from db.models import SubscriptionTier

        try:
            scope = (data.get('scope') or 'user').lower()
            user = None
            if data.get('user_id'):
                user = db_session.query(User).filter(User.user_id == data['user_id']).first()
            group = None
            if scope == 'group' and data.get('group_id'):
                group = db_session.query(Group).filter(Group.group_id == data['group_id']).first()
            tier = None
            if data.get('tier_key'):
                tier = db_session.query(SubscriptionTier).filter(SubscriptionTier.key == data['tier_key']).first()

            amount = format_money(data.get('amount_cents'), data.get('currency') or 'USD')
            interval = tier.interval if tier and tier.interval else 'month'
            per = f"{amount}/{'yr' if interval == 'year' else 'mo'}"
            tier_name = tier.name if tier else 'Supporter'

            display_name = None
            if user:
                if user.players:
                    display_name = user.players[0].player_name
                display_name = display_name or user.username
            mention = f"<@{user.discord_id}>" if user and user.discord_id else None
            supporter_text = display_name or mention or "An anonymous supporter"
            group_link = (
                f"[{group.group_name}](https://www.droptracker.io/groups/{group.group_name}.{group.group_id}/view)"
                if group else None
            )

            sent_any = False
            errors = []

            # 1) Global supporters channel
            if group_link:
                headline = f"**{supporter_text}** just contributed **{per}** toward {group_link}'s premium subscription. Thank you for supporting DropTracker!"
            else:
                headline = f"**{supporter_text}** just became a DropTracker supporter with a **{per}** contribution. Thank you for keeping the project alive!"
            global_embed = interactions.Embed(
                title=f"{SUPPORTER_EMOJI} New supporter contribution!",
                description=headline,
                color=CONTRIBUTION_COLOR,
            )
            global_embed.add_field(name="Tier", value=tier_name, inline=True)
            global_embed.add_field(name="Supporting", value=group_link or "DropTracker Premium", inline=True)
            global_embed.set_thumbnail(BRAND_THUMBNAIL)
            global_embed.set_footer(global_footer)
            try:
                channel = await self.bot.fetch_channel(CONTRIBUTION_CHANNEL_ID)
                if channel:
                    await channel.send(embed=global_embed)
                    sent_any = True
            except Exception as e:
                errors.append(f"global channel: {e}")
                app_logger.log(log_type="error", data=f"Error sending contribution announcement to global channel: {e}", app_name="notification_service", description="send_contribution_notification")

            # 2) Thank-you DM to the contributor
            if user and user.discord_id:
                if group:
                    dm_description = f"Your **{per}** contribution toward **{group.group_name}**'s premium subscription has been received."
                    manage_url = f"https://www.droptracker.io/groups/{group.group_id}/subscription"
                else:
                    dm_description = f"Your **{per}** supporter subscription is now active."
                    manage_url = "https://www.droptracker.io/premium"
                dm_embed = interactions.Embed(
                    title=f"{DROPTRACKER_EMOJI} Thank you for your support!",
                    description=dm_description,
                    color=CONTRIBUTION_COLOR,
                )
                dm_embed.add_field(
                    name="Manage your subscription",
                    value=f"[View or change it any time]({manage_url}) — and if you have questions, [reach out in our Discord](https://www.droptracker.io/discord).",
                )
                dm_embed.set_thumbnail(BRAND_THUMBNAIL)
                dm_embed.set_footer(global_footer)
                try:
                    discord_user = await self.bot.fetch_user(user.discord_id)
                    if discord_user:
                        await discord_user.send(embed=dm_embed)
                        sent_any = True
                except Exception as e:
                    errors.append(f"contributor DM: {e}")
                    app_logger.log(log_type="error", data=f"Error DMing contributor {user.user_id}: {e}", app_name="notification_service", description="send_contribution_notification")

            # 3) The group's own Discord (group contributions only)
            if group and group.guild_id:
                group_embed = interactions.Embed(
                    title=f"{SUPPORTER_EMOJI} {group.group_name} received a contribution!",
                    description=f"{mention or supporter_text} contributed **{per}** toward the group's premium subscription — keeping premium perks unlocked for everyone.",
                    color=CONTRIBUTION_COLOR,
                )
                group_embed.set_thumbnail(BRAND_THUMBNAIL)
                group_embed.set_footer(global_footer)
                try:
                    guild = await self.bot.fetch_guild(group.guild_id)
                    guild_channel = guild.public_updates_channel if guild else None
                    if guild_channel:
                        await guild_channel.send(embed=group_embed)
                        sent_any = True
                except Exception as e:
                    errors.append(f"group channel: {e}")
                    app_logger.log(log_type="error", data=f"Error sending contribution notice to group {group.group_id}: {e}", app_name="notification_service", description="send_contribution_notification")

            if sent_any:
                notification.status = 'sent'
                notification.processed_at = datetime.now()
            else:
                notification.status = 'failed'
                notification.error_message = "; ".join(errors) or "No deliverable destination"
            db_session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            app_logger.log(log_type="error", data=f"Error sending contribution notification: {e}", app_name="notification_service", description="send_contribution_notification")
            raise

    def _nitro_pick_components(self, context: dict):
        """Clan-picker select for a booster in >1 group; empty otherwise."""
        groups = context.get("groups") or []
        if len(groups) < 2:
            return []
        picked = context.get("picked_group_id")
        options = [
            interactions.StringSelectOption(
                label=str(g["name"])[:100], value=str(g["id"]), default=(g["id"] == picked)
            )
            for g in groups[:25]
        ]
        return [
            interactions.ActionRow(
                interactions.StringSelectMenu(
                    options,
                    custom_id="nitro_pick",
                    placeholder="Choose which clan your boost supports",
                    min_values=1,
                    max_values=1,
                )
            )
        ]

    async def send_nitro_boost_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Confirm a Nitro boost to the booster (DM, with a clan picker when they
        belong to multiple groups) and — when ``announce`` — post a one-liner to
        the contributors channel. Sent by the main bot (more mutual guilds, more
        recognizable than the webhook bot that detected the boost)."""
        from services import nitro_attribution
        try:
            discord_id = str(data.get("discord_id") or "")
            announce = bool(data.get("announce", True))
            context = (
                nitro_attribution.booster_context(db_session, discord_id)
                if discord_id else {"linked": False, "groups": []}
            )
            sent_any = False
            errors = []

            # 1) Confirmation DM to the booster (with the clan picker if multi-group).
            if discord_id:
                try:
                    discord_user = await self.bot.fetch_user(discord_id)
                    if discord_user:
                        await discord_user.send(
                            content=nitro_attribution.nitro_boost_dm_text(context),
                            components=self._nitro_pick_components(context),
                        )
                        sent_any = True
                except Exception as e:
                    errors.append(f"booster DM: {e}")
                    app_logger.log(log_type="error", data=f"Error DMing booster {discord_id}: {e}", app_name="notification_service", description="send_nitro_boost_notification")

            # 2) One-liner in the contributors channel.
            if announce:
                try:
                    channel = await self.bot.fetch_channel(CONTRIBUTION_CHANNEL_ID)
                    if channel:
                        await channel.send(
                            nitro_attribution.nitro_boost_announcement_text(f"<@{discord_id}>", context)
                        )
                        sent_any = True
                except Exception as e:
                    errors.append(f"contributors channel: {e}")
                    app_logger.log(log_type="error", data=f"Error announcing boost to contributors channel: {e}", app_name="notification_service", description="send_nitro_boost_notification")

            notification.status = 'sent' if sent_any else 'failed'
            notification.error_message = None if sent_any else ("; ".join(errors) or "No deliverable destination")
            notification.processed_at = datetime.now()
            db_session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            app_logger.log(log_type="error", data=f"Error sending nitro boost notification: {e}", app_name="notification_service", description="send_nitro_boost_notification")
            raise

    async def send_nitro_boost_summary_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Post ONE consolidated Nitro-boost thank-you (the retroactive backfill)
        to the contributors channel — a single message listing all boosters,
        not one per person."""
        from services import nitro_attribution
        try:
            entries = data.get("entries") or []
            credited_cents = int(data.get("credited_cents") or 0)
            blocks = nitro_attribution.nitro_boost_summary_blocks(entries, credited_cents)
            embeds = []
            for i, block in enumerate(blocks):
                if i == 0:
                    embed = interactions.Embed(
                        title="🚀 Thank you to our Server Boosters!",
                        description=block,
                        color=CONTRIBUTION_COLOR,
                    )
                    embed.set_thumbnail(BRAND_THUMBNAIL)
                else:
                    embed = interactions.Embed(description=block, color=CONTRIBUTION_COLOR)
                embed.set_footer(global_footer)
                embeds.append(embed)

            sent = False
            error = None
            if embeds:
                try:
                    channel = await self.bot.fetch_channel(CONTRIBUTION_CHANNEL_ID)
                    if channel:
                        await channel.send(embeds=embeds)
                        sent = True
                except Exception as e:
                    error = f"contributors channel: {e}"
                    app_logger.log(log_type="error", data=f"Error posting nitro boost summary: {e}", app_name="notification_service", description="send_nitro_boost_summary")

            notification.status = 'sent' if sent else 'failed'
            notification.error_message = None if sent else (error or "No boosters to announce")
            notification.processed_at = datetime.now()
            db_session.commit()
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            app_logger.log(log_type="error", data=f"Error sending nitro boost summary: {e}", app_name="notification_service", description="send_nitro_boost_summary")
            raise

    async def send_drop_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a drop notification to Discord with session"""
        from db.models import NotifiedSubmission
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            #print(f"Got raw drop notification data: {data}")
            drop_id = data.get('drop_id')


            
            # Get channel ID for this group
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
            ).first()

            existing_notification = db_session.query(NotifiedSubmission).filter(
                NotifiedSubmission.player_id == player_id,
                NotifiedSubmission.group_id == group_id,
                NotifiedSubmission.drop_id == drop_id
            ).first()
            if existing_notification:
                print(f"Drop was already notified... Skipping")
                return
            
            if not channel_id_config:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return
            
            channel_id = channel_id_config.config_value
            channel = None
            channel_error = None
            if channel_id != "":
                channel, channel_error = await self._fetch_sendable_channel(channel_id)
            else:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return
            
            # Get player name
            player_name = data.get('player_name')
            item_name = data.get('item_name')
            kill_count = data.get('kill_count', None)
            item_id = db_session.query(ItemList).filter(ItemList.item_name == item_name).first()
            if item_id:
                item_id = item_id.item_id
            else:
                item_id = 1
            npc_name = data.get('npc_name', None)
            if npc_name:
                npc_id = db_session.query(NpcList).filter(NpcList.npc_name == npc_name).first()
            else:
                npc_id = 0
            if npc_id:
                npc_id = npc_id.npc_id
            else:
                npc_id = 1
            value = data.get('value')
            quantity = data.get('quantity')
            total_value = data.get('total_value')
            image_url = data.get('image_url', None)
            if image_url is None or image_url == "":
                try:
                    drop = db_session.query(Drop).filter(Drop.drop_id == data.get('drop_id')).first()
                    if drop:
                        image_url = drop.image_url
                except Exception as e:
                    image_url = None
            # Remote (non-droptracker) URLs are handled too — the resolver
            # fetches them to a temp file so the screenshot still attaches
            # (they used to be discarded here, which is why low-value non-API
            # drops posted imageless embeds).
            if not image_url:
                image_url = ""

            # Best-effort video URL (may be empty if still processing)
            video_url = ""
            try:
                drop = db_session.query(Drop).filter(Drop.drop_id == data.get('drop_id')).first()
                if drop and getattr(drop, "video_url", None):
                    video_url = drop.video_url
            except Exception:
                pass
            if not video_url:
                video_url = self._maybe_get_video_url(db_session, data)
            
            # Get embed template
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('drop', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('drop', 1)
            #print(f"Debug - embed_template: {embed_template}")
            
            if not embed_template:
                notification.status = 'failed'
                notification.error_message = f"No embed template for group {group_id}"
                db_session.commit()
                return
            
            # Resolve the screenshot to an attachable file (local for hosted
            # URLs, temp-download for remote ones — cleaned up after send).
            attachment, image_temp_path = await self._resolve_image_attachment(image_url, notification.id)
            
            # Replace placeholders in embed
            player = None
            if not player_id:
                player = db_session.query(Player).filter(Player.player_name == player_name).first()
                if player:
                    player_id = player.player_id
            
            partition = get_current_partition()
            # Use monthly total computed by redis_updates player cache
            month_total_int = self._get_player_month_total(player_id, partition)
            player_month_total = format_number(month_total_int)
            players_in_group = db_session.query(Player.player_id).join(Player.groups).filter(Group.group_id == group_id).all()
            group_month_total = format_number(get_player_list_loot_sum([player.player_id for player in players_in_group]))
            # Use centralized rank helper for accuracy
            global_rank_data = loot_tracker.get_player_rank(player_id, None, partition)
            group_rank_data = loot_tracker.get_player_rank(player_id, group_id, partition)
            #print(f"Got group rank data: {group_rank_data}")
            print(f"Got global rank data: {global_rank_data}")
            if group_rank_data:
                group_rank, user_count = group_rank_data
            else:
                group_rank, user_count = None, redis_client.client.zcard(f"leaderboard:{partition}:group:{group_id}")
            if global_rank_data:
                global_rank, total_global_players = global_rank_data
            else:
                global_rank, total_global_players = None, redis_client.client.zcard(f"leaderboard:{partition}")
            # get all group ranks
            all_groups = db_session.query(Group.group_id).filter(Group.group_id != 2).all()
            #all_groups = db_session.query(Group.group_id).all()
            total_groups = len(all_groups) - 1
            group_totals = []
            for group in all_groups:
                group_total = redis_client.zsum(f"leaderboard:{partition}:group:{group.group_id}")
                group_totals.append({'id': group.group_id,
                                   'total': group_total})
            sorted_groups = sorted(group_totals, key=lambda x: x['total'], reverse=True)
            group_to_group_rank = str(next((i for i, g in enumerate(sorted_groups) if g['id'] == group_id), 0) + 1)
            formatted_name = get_formatted_name(player_name, group_id, db_session)
            content = f"{formatted_name} received a drop:"
            # Build rank strings safely
            if global_rank is not None and total_global_players is not None:
                global_rank_str = "`" + str(global_rank) + "`" + "/" + "`" + str(total_global_players) + "`"
            else:
                global_rank_str = "`?`"
            if group_rank is not None and user_count is not None:
                group_rank_str = "`" + str(group_rank) + "`" + "/" + "`" + str(user_count) + "`"
            else:
                group_rank_str = "`?`"

            ##
            # Embed may build for items dropped in quantities > 1 if the group has it enabled
            # in this case, we should specify in the message how many of the item they received
            # if the item is dropped in a quantity of 1, we do not need to modify the embed
            # We just use a simple string replacement to insert the quantity into the item name before sending to replace_placeholders
            if quantity > 1:
                quantity_text = f"{str(quantity)} `x` {item_name}"
                item_value = f"`{format_number(total_value)}` ({quantity} `x` {format_number(total_value / quantity)})"

            values = {
                "{item_name}": item_name,
                "{month_name}": datetime.now().strftime("%B"),
                "{player_total_month}": "`" + player_month_total + "`",
                "{global_rank}": global_rank_str,
                "{group_rank}": group_rank_str,
                "{group_total}": "`" + str(group_total) + "`",
                "{user_count}": "`" + str(user_count) + "`",
                "{group_total_month}": "`" + group_month_total + "`",
                "{group_to_group_rank}": "`" + str(group_to_group_rank) + "`" + "/" + "`" + str(total_groups) + "`",
                "{item_id}": str(item_id),
                "{npc_id}": str(npc_id),
                "{npc_name}": npc_name,
                "{kill_count}": str(kill_count),
                "{item_value}": "`" + format_number(total_value) + "`",
                "{quantity}": "`" + str(quantity) + "`",
                "{total_value}": "`" + str(total_value) + "`",
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                # Prefer video for display; keep image_url for attachments/local files.
                "{image_url}": video_url or image_url or "",
                "{video_url}": video_url or "",
                "{video_link}": f"[Video]({video_url})" if video_url else "",
            }
            values.update(self._group_points_placeholder_map(data))
            values.update(self._plugin_version_placeholder_map(data))
            #print("Sending to replace_placeholders")
            
            embed = replace_placeholders(embed_template, values)
            embed = self._finalize_group_points_embed(embed)
            if group_id == 2:
                embed = await self.remove_group_field(embed)
            if kill_count is None or int(kill_count) < 1:
                embed = await self.remove_kc_field(embed)
            # (The old second attach pass lived here; _resolve_image_attachment
            # above already covers both hosted and remote URLs.)
            #print("Got the embed...")
            # Prefer attaching MP4 if available (Discord renders as native video)
            video_attachment, video_local_path = (None, None)
            if video_url:
                video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)

            try:
                if video_attachment:
                    message = await channel.send(content, embed=embed, files=video_attachment)
                elif attachment:
                    message = await channel.send(content, embed=embed, files=attachment)
                else:
                    message = await channel.send(content, embed=embed)
            finally:
                if video_local_path:
                    try:
                        os.remove(video_local_path)
                    except Exception:
                        pass
                if image_temp_path:
                    try:
                        os.remove(image_temp_path)
                    except Exception:
                        pass

            # Mark as sent
            await self._cleanup_processed_local_video_after_send(db_session, data)
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            
            # Create NotifiedSubmission entry
            from db.models import Drop
            drop = db_session.query(Drop).filter(Drop.drop_id == data.get('drop_id')).first()
            
            if drop and message:
                notified_sub = NotifiedSubmission(
                    channel_id=str(message.channel.id),
                    player_id=player_id,
                    message_id=str(message.id),
                    group_id=group_id,
                    status="sent",
                    drop=drop
                )
                db_session.add(notified_sub)
            
            db_session.commit()

        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_new_npc_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send notification about new NPC with session"""
        try:
            npc_name = data.get('npc_name')
            player_name = data.get('player_name')
            item_name = data.get('item_name')
            value = data.get('value')
            
            await confirm_new_npc(self.bot, npc_name, player_name, item_name, value)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_new_item_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send notification about new item with session"""
        try:
            item_name = data.get('item_name')
            player_name = data.get('player_name')
            item_id = data.get('item_id')
            npc_name = data.get('npc_name')
            value = data.get('value')
            
            await confirm_new_item(self.bot, item_name, player_name, item_id, npc_name, value)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_name_change_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send notification about player name change with session"""
        try:
            player_name = data.get('player_name')
            player_id = data.get('player_id')
            old_name = data.get('old_name')
            
            await name_change_message(self.bot, player_name, player_id, old_name)

            # The user-facing DM is handled by the separate dm_name_change
            # queue row, gated on the `dm_account_changes` opt-in setting.

            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    def _user_dm_config(self, db_session, user_id: int, key: str):
        row = db_session.query(UserConfiguration).filter(
            UserConfiguration.user_id == user_id,
            UserConfiguration.config_key == key
        ).first()
        return row.config_value if row else None

    async def send_submission_dm_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """DM the owning user about their own submission (dm_* queue types).

        Ineligible rows are marked sent with a reason instead of failed — the
        DM is a best-effort perk, and a skip must not retry or alert admins.
        Submission DMs require the `dm_submissions` supporter entitlement;
        dm_name_change is a free feature gated only by its opt-in config.
        """
        def _skip(reason: str):
            notification.status = 'sent'
            notification.error_message = reason
            notification.processed_at = datetime.now()
            db_session.commit()

        ntype = notification.notification_type
        player = db_session.query(Player).filter(Player.player_id == notification.player_id).first()
        user = player.user if player else None
        if not user or not user.discord_id:
            return _skip("skipped: no linked Discord user")
        if user.never_ping:
            return _skip("skipped: user has never_ping")

        if ntype != 'dm_name_change':
            from db.entitlements import user_has_entitlement
            if not user_has_entitlement(user.user_id, 'dm_submissions'):
                return _skip("skipped: no dm_submissions entitlement")

        if ntype == 'dm_drop':
            min_raw = self._user_dm_config(db_session, user.user_id, 'dm_min_value')
            try:
                min_value = int(min_raw) if min_raw else 0
            except (TypeError, ValueError):
                min_value = 0
            try:
                total_value = int(data.get('total_value') or 0)
            except (TypeError, ValueError):
                total_value = 0
            if total_value < min_value:
                return _skip(f"skipped: below dm_min_value ({total_value} < {min_value})")

        embed = self._build_submission_dm_embed(ntype, data, db_session)
        if embed is None:
            return _skip(f"skipped: no DM format for {ntype}")

        try:
            discord_user = await self.bot.fetch_user(user_id=user.discord_id)
            if not discord_user:
                return _skip("skipped: Discord user not found")
            await discord_user.send(embed=embed)
        except interactions.errors.Forbidden:
            # User has DMs closed — don't fail (would ping group admins
            # upstream). Record it so the website can prompt them to open
            # DMs from server members (cleared on the next successful DM).
            self._set_dm_delivery_issue(db_session, user.user_id, True)
            return _skip("skipped: user's DMs are closed")

        self._set_dm_delivery_issue(db_session, user.user_id, False)
        notification.status = 'sent'
        notification.processed_at = datetime.now()
        db_session.commit()

    def _set_dm_delivery_issue(self, db_session, user_id: int, failed: bool) -> None:
        """Maintain the `dm_delivery_issue` user config flag (site banner)."""
        try:
            row = db_session.query(UserConfiguration).filter(
                UserConfiguration.user_id == user_id,
                UserConfiguration.config_key == 'dm_delivery_issue'
            ).first()
            if failed:
                if row is None:
                    db_session.add(UserConfiguration(
                        user_id=user_id, config_key='dm_delivery_issue', config_value='true'
                    ))
                else:
                    row.config_value = 'true'
            elif row is not None and str(row.config_value).lower() in ('true', '1'):
                row.config_value = 'false'
        except Exception:
            pass

    def _build_submission_dm_embed(self, ntype: str, data: dict, db_session):
        """Small personal embed per submission type; None for unknown types."""
        player_name = data.get('player_name', 'your account')
        embed = None
        thumb = None

        if ntype == 'dm_drop':
            item_name = data.get('item_name', 'Unknown item')
            quantity = data.get('quantity') or 1
            npc_name = data.get('npc_name', 'Unknown source')
            total_value = data.get('total_value') or 0
            qty_text = f" x{quantity}" if str(quantity) not in ("1", "None") else ""
            embed = interactions.Embed(
                title="You received a drop!",
                description=f"**{item_name}**{qty_text} from **{npc_name}** ({format_number(total_value)} gp)",
                color="#FFD700",
            )
            if data.get('kill_count'):
                embed.add_field(name="Kill count", value=str(data['kill_count']), inline=True)
            item = db_session.query(ItemList).filter(ItemList.item_name == item_name).first()
            if item:
                thumb = f"https://www.droptracker.io/img/itemdb/{item.item_id}.png"
        elif ntype == 'dm_pb':
            boss_name = data.get('boss_name', 'Unknown boss')
            time_ms = data.get('kill_time_ms') or data.get('time_ms')
            time_text = convert_from_ms(time_ms) if time_ms else "?"
            embed = interactions.Embed(
                title="New personal best!",
                description=f"**{boss_name}** in **{time_text}**",
                color="#00f0f0",
            )
            if data.get('team_size'):
                embed.add_field(name="Team size", value=str(data['team_size']), inline=True)
            old_ms = data.get('old_time_ms')
            if old_ms:
                embed.add_field(name="Previous best", value=convert_from_ms(old_ms), inline=True)
        elif ntype == 'dm_ca':
            embed = interactions.Embed(
                title="Combat achievement completed!",
                description=f"**{data.get('task_name', 'Unknown task')}** ({data.get('tier', '?')} tier)",
                color="#E67E22",
            )
            if data.get('points_total'):
                embed.add_field(name="Total points", value=str(data['points_total']), inline=True)
            if data.get('completed_tier'):
                embed.add_field(name="Tier completed", value=str(data['completed_tier']), inline=True)
        elif ntype == 'dm_clog':
            item_name = data.get('item_name', 'Unknown item')
            embed = interactions.Embed(
                title="New collection log slot!",
                description=f"**{item_name}** from **{data.get('npc_name', 'Unknown source')}**",
                color="#9B59B6",
            )
            if data.get('kc_received'):
                embed.add_field(name="Kill count", value=str(data['kc_received']), inline=True)
            if data.get('item_id'):
                thumb = f"https://www.droptracker.io/img/itemdb/{data['item_id']}.png"
        elif ntype == 'dm_pet':
            source = data.get('npc_name') or data.get('source') or 'Unknown source'
            embed = interactions.Embed(
                title="You got a pet!",
                description=f"**{data.get('pet_name', 'A pet')}** from **{source}**",
                color="#2ECC71",
            )
            if data.get('killcount'):
                embed.add_field(name="Kill count", value=str(data['killcount']), inline=True)
            if data.get('duplicate'):
                embed.add_field(name="Duplicate", value="Yes", inline=True)
        elif ntype == 'dm_quest':
            embed = interactions.Embed(
                title="Quest completed!",
                description=f"**{data.get('quest_name', 'Unknown quest')}**",
                color="#3498DB",
            )
            if data.get('quests_completed') and data.get('total_quests'):
                embed.add_field(
                    name="Progress",
                    value=f"{data['quests_completed']}/{data['total_quests']} quests",
                    inline=True,
                )
            if data.get('total_quest_points'):
                embed.add_field(name="Quest points", value=str(data['total_quest_points']), inline=True)
        elif ntype == 'dm_death':
            source = data.get('source') or 'Unknown cause'
            location = data.get('location')
            loc_text = f" at **{location}**" if location else ""
            embed = interactions.Embed(
                title="You died...",
                description=f"**{player_name}** died to **{source}**{loc_text}",
                color="#E74C3C",
            )
        elif ntype == 'dm_diary':
            embed = interactions.Embed(
                title="Achievement diary completed!",
                description=f"**{data.get('diary_name', 'Unknown diary')}** ({data.get('diary_tier', '?')})",
                color="#1ABC9C",
            )
        elif ntype == 'dm_level_up':
            skills_text = data.get('skills_text') or data.get('skill_name') or 'a skill'
            embed = interactions.Embed(
                title="Level up!",
                description=f"**{player_name}** advanced: {skills_text}",
                color="#F1C40F",
            )
            if data.get('total_level'):
                embed.add_field(name="Total level", value=str(data['total_level']), inline=True)
            if data.get('combat_level'):
                embed.add_field(name="Combat level", value=str(data['combat_level']), inline=True)
        elif ntype == 'dm_name_change':
            embed = interactions.Embed(
                title="Name change detected:",
                description=f"Your account, {data.get('old_name')}, has changed names to {player_name}.",
                color="#00f0f0",
            )
            embed.add_field(
                name="Is this a mistake?",
                value="Reach out in [our discord](https://www.droptracker.io/discord)",
            )

        if embed is None:
            return None
        if thumb:
            embed.set_thumbnail(url=thumb)
        embed.set_footer(global_footer)
        return embed

    async def send_new_player_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send notification about new player with session"""
        try:
            player_name = data.get('player_name')
            
            await new_player_message(self.bot, player_name)
            
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_pb_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a personal best notification to Discord with session"""
        from db.models import NotifiedSubmission
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            
            # Get channel ID for this group
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_pb'
            ).first()
            pb_id = data.get('pb_id', None)
            if pb_id:
                existing_notification = db_session.query(NotifiedSubmission).filter(
                    NotifiedSubmission.player_id == player_id,
                    NotifiedSubmission.group_id == group_id,
                    NotifiedSubmission.pb_id == pb_id
                ).first()
                if existing_notification:
                    print(f"PB was already notified... Skipping")
                    return
            
            
            if not channel_id_config:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return
            
            channel = None
            channel_error = None
            channel_id = channel_id_config.config_value
            if channel_id != "":
                channel, channel_error = await self._fetch_sendable_channel(channel_id)
            else:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
                if channel_id_config:
                    channel_id = channel_id_config.config_value
                    channel, channel_error = await self._fetch_sendable_channel(channel_id)
                else:
                    notification.status = 'failed'
                    notification.error_message = f"No channel configured for group {group_id}"
                    db_session.commit()
                    return
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return
            # hall_of_fame = self.bot.get_ext("services.hall_of_fame")
            # if hall_of_fame:
            #     try:
            #         await hall_of_fame.update_boss_component(group_id, npc_id)
            #     except Exception as e:
            #         print(f"Error updating boss component: {e}")
            #         pass
            # Get data
            player_name = data.get('player_name')
            boss_name = data.get('boss_name')
            time_ms = data.get('time_ms')
            old_time_ms = data.get('old_time_ms')
            kill_time_ms = data.get('kill_time_ms')
            image_url = data.get('image_url')
            team_size = data.get('team_size')
            npc_id = data.get('npc_id')
            # Format times
            time_formatted = convert_from_ms(time_ms)
            old_time_formatted = convert_from_ms(old_time_ms) if old_time_ms else None
            
            # Get embed template
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('pb', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('pb', 1)
            if group_id == 2:
                embed_template = await self.remove_group_field(embed_template)
            
            #print(f"Debug - embed_template: {embed_template}")
            partition = get_current_partition()
            player_total_raw = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
            group_wom_id = db_session.query(Group.wom_id).filter(Group.group_id == group_id).first()
            if group_wom_id:
                group_wom_id = group_wom_id[0]
            wom_member_list = []
            if group_wom_id:
                #print("Finding group members?")
                try:
                    wom_member_list = await fetch_group_members(wom_group_id=int(group_wom_id), session_to_use=db_session)
                except Exception as e:
                    #print("Couldn't get the member list", e)
                    return
            player_ids = await associate_player_ids(wom_member_list)
            
            group_ranks = db_session.query(PersonalBestEntry).filter(PersonalBestEntry.player_id.in_(player_ids), PersonalBestEntry.npc_id == int(npc_id),
                                                                        PersonalBestEntry.team_size == team_size).order_by(PersonalBestEntry.personal_best.asc()).all()
            all_ranks = db_session.query(PersonalBestEntry).filter(PersonalBestEntry.npc_id == int(npc_id),
                                                                    PersonalBestEntry.team_size == team_size).order_by(PersonalBestEntry.personal_best.asc()).all()
                #print("Group ranks:",group_ranks)
                #print("All ranks:",all_ranks)
            total_ranked_group = len(group_ranks)
            total_ranked_global = len(all_ranks)
            current_user_best_ms = time_ms
                ## player's rank in group
            group_placement = None
            global_placement = None
            #print("Assembling rankings....")
            ## For some reason, players occassionally don't appear in group rank listings...
            if str(player_id) not in [str(entry.player_id) for entry in group_ranks]:
                # Find where this time would be inserted in the sorted list
                group_placement = len(group_ranks) + 1  # Default to last place (worst time)
                for idx, entry in enumerate(group_ranks, start=1):
                    if current_user_best_ms <= entry.personal_best:
                        # Current user's time is faster or equal, so they rank at this position
                        group_placement = idx
                        break
            else:
                for idx, entry in enumerate(group_ranks, start=1): 
                    if entry.personal_best == current_user_best_ms:
                        group_placement = idx
                        break
            ## player's rank globally
            global_placement = len(all_ranks) + 1  # Default to last place (worst time)
            for idx, entry in enumerate(all_ranks, start=1):
                if current_user_best_ms <= entry.personal_best:
                    global_placement = idx
                    break
            if group_placement is None:
                group_placement = "`?`"
                # Replace placeholders
            formatted_name = get_formatted_name(player_name, group_id, db_session)
            
            replacements = {
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                "{global_rank}": str(global_placement),
                "{total_ranked_global}": str(total_ranked_global),
                "{group_rank}": str(group_placement),
                "{total_ranked_group}": str(total_ranked_group),
                "{npc_name}": boss_name,
                "{npc_id}": str(npc_id),
                "{team_size}": team_size,
                "{personal_best}": time_formatted,
            }
            replacements.update(self._group_points_placeholder_map(data))
            replacements.update(self._plugin_version_placeholder_map(data))
            video_url = self._maybe_get_video_url(db_session, data)
            replacements["{video_url}"] = video_url or ""
            replacements["{video_link}"] = f"[Video]({video_url})" if video_url else ""
            # Prefer video for display; keep screenshot in data["image_url"] for attachments
            replacements["{image_url}"] = video_url or (data.get("image_url") or "")
            
            embed = replace_placeholders(embed_template, replacements)
            embed = self._finalize_group_points_embed(embed)
            
            # Send message
            content = f"{formatted_name} has achieved a new personal best:"
            video_attachment, video_local_path = (None, None)
            if video_url:
                video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)

            try:
                if video_attachment:
                    message = await channel.send(content, embed=embed, files=video_attachment)
                elif image_url:
                    try:
                        local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                        if os.path.exists(local_path):
                            attachment = interactions.File(local_path)
                            message = await channel.send(content, embed=embed, files=attachment)
                        else:
                            message = await channel.send(content, embed=embed)
                    except Exception:
                        message = await channel.send(content, embed=embed)
                else:
                    message = await channel.send(content, embed=embed)
            finally:
                if video_local_path:
                    try:
                        os.remove(video_local_path)
                    except Exception:
                        pass
            
            await self._cleanup_processed_local_video_after_send(db_session, data)
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_pet_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a pet notification to Discord with session"""
        from db.models import NotifiedSubmission
        group_id = notification.group_id
        player_id = notification.player_id
        print("Got pet data:", data)
        pet_name = data.get('pet_name')
        source = data.get('source')
        npc_name = data.get('npc_name')
        killcount = data.get('killcount')
        milestone = data.get('milestone')
        duplicate = data.get('duplicate')
        previously_owned = data.get('previously_owned')
        game_message = data.get('game_message')
        image_url = data.get('image_url')
        item_id = data.get('item_id')
        npc_id = data.get('npc_id')
        is_new_pet = data.get('is_new_pet')
        group_id = data.get('group_id')
        player_name = data.get('player_name')
        update_active = has_custom_embeds(group_id)
        if update_active:
            embed_template = await self.db_ops.get_group_embed('pet', group_id)
        else:
            embed_template = await self.db_ops.get_group_embed('pet', 1)
        
        if not embed_template:
            notification.status = 'failed'
            notification.error_message = f"No embed template for group {group_id}"
            db_session.commit()
            return
        
        
        channel_id_config = db_session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key == 'channel_id_to_post_pets'
        ).first()
        
        
        if not channel_id_config:
            notification.status = 'failed'
            notification.error_message = f"No channel configured for group {group_id}"
            db_session.commit()
            return
        kc_received = milestone if milestone else killcount
        
        value_dict = {
            "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
            "{pet_name}": pet_name,
            "{source}": source,
            "{npc_name}": npc_name,
            "{killcount}": kc_received, 
            "{milestone}": kc_received,
            "{duplicate}": duplicate,
            "{previously_owned}": previously_owned
        }
        value_dict.update(self._group_points_placeholder_map(data))
        value_dict.update(self._plugin_version_placeholder_map(data))
        video_url = self._maybe_get_video_url(db_session, data)
        value_dict["{video_url}"] = video_url or ""
        value_dict["{video_link}"] = f"[Video]({video_url})" if video_url else ""
        # Prefer video for display; keep screenshot in data["image_url"] for attachments
        value_dict["{image_url}"] = video_url or (data.get("image_url") or "")
        try:
            channel, channel_error = await self._fetch_sendable_channel(channel_id_config.config_value)
            formatted_name = get_formatted_name(player_name, group_id, db_session)
            if channel:
                embed = replace_placeholders(embed_template, value_dict)
                embed = self._finalize_group_points_embed(embed)
                if group_id == 2:
                    embed = await self.remove_group_field(embed)
                
                content = f"{formatted_name} has acquired a new pet!"
                video_attachment, video_local_path = (None, None)
                if video_url:
                    video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)

                try:
                    if video_attachment:
                        message = await channel.send(content, embed=embed, files=video_attachment)
                    elif image_url:
                        try:
                            local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                            if os.path.exists(local_path):
                                attachment = interactions.File(local_path)
                                message = await channel.send(content, embed=embed, files=attachment)
                            else:
                                message = await channel.send(content, embed=embed)
                        except Exception:
                            message = await channel.send(content, embed=embed)
                    else:
                        message = await channel.send(content, embed=embed)
                finally:
                    if video_local_path:
                        try:
                            os.remove(video_local_path)
                        except Exception:
                            pass
                
                await self._cleanup_processed_local_video_after_send(db_session, data)
                notification.status = 'sent'
                notification.processed_at = datetime.now()
                db_session.commit()
                return
            notification.status = 'failed'
            notification.error_message = channel_error or f"Channel not found for group {group_id}"
            db_session.commit()
            return
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = f"Failed to send pet notification: {e}"
            db_session.commit()
            return

    async def send_ca_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a combat achievement notification to Discord with session"""
        from db.models import NotifiedSubmission
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            #print("Got raw CA data:", data)
            
            # Get channel ID for this group
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_ca'
            ).first()
            
            if not channel_id_config:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return
            
            ca_id = data.get('ca_id', None)
            if ca_id:
                existing_notification = db_session.query(NotifiedSubmission).filter(
                    NotifiedSubmission.player_id == player_id,
                    NotifiedSubmission.group_id == group_id,
                    NotifiedSubmission.ca_id == ca_id
                ).first()
                if existing_notification:
                    print(f"CA was already notified... Skipping")
                    return
            
            channel = None
            channel_error = None
            channel_id = channel_id_config.config_value
            if channel_id != "":
                channel, channel_error = await self._fetch_sendable_channel(channel_id)
            else:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
                if channel_id_config:
                    channel_id = channel_id_config.config_value
                    channel, channel_error = await self._fetch_sendable_channel(channel_id)
                else:
                    notification.status = 'failed'
                    notification.error_message = f"No channel configured for group {group_id}"
                    db_session.commit()
                    return
            if channel is None:
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return
            # Get data
            player_name = data.get('player_name')
            task_name = data.get('task_name')
            task_tier = data.get('tier')
            image_url = data.get('image_url')
            points_awarded = data.get('points_awarded')
            points_total = data.get('points_total')
            
            # Map tier to color and name
            tier_colors = {
                "1": 0x00ff00,  # Easy - Green
                "2": 0x0000ff,  # Medium - Blue
                "3": 0xff0000,  # Hard - Red
                "4": 0xffff00,  # Elite - Yellow
                "5": 0xff00ff,  # Master - Purple
                "6": 0x00ffff   # Grandmaster - Cyan
            }
            
            tier_names = {
                "1": "Easy",
                "2": "Medium",
                "3": "Hard",
                "4": "Elite",
                "5": "Master",
                "6": "Grandmaster"
            }
            
            # Get embed template
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('ca', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('ca', 1)
            
            async with osrs_api.create_client() as client:
                actual_tier = await client.semantic.get_current_ca_tier(points_total)
            #actual_tier = await get_current_ca_tier(points_total)
            tier_order = ['Grandmaster', 'Master', 'Elite', 'Hard', 'Medium', 'Easy']
            if actual_tier is None:
                next_tier = "Easy"
            else:
                next_tier = tier_order[tier_order.index(actual_tier) - 1]
            async with osrs_api.create_client() as client:
                progress, next_tier_points = await client.semantic.get_ca_tier_progress(points_total)
            #progress, next_tier_points = await get_ca_tier_progress(points_total)
            formatted_task_name = task_name.replace(" ", "_").replace("?", "%3F")
            wiki_url = f"https://oldschool.runescape.wiki/w/{formatted_task_name}"
            formatted_task_name = f"[{task_name}]({wiki_url})"
            try:
                if not next_tier_points or next_tier_points == 0:
                    next_tier_points = 38
                points_left = int(next_tier_points) - int(points_total)
            except Exception as e:
                points_left = "Unknown"
            if embed_template:
                value_dict = {
                    "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                    "{task_name}": formatted_task_name,
                    "{current_tier}": actual_tier,
                    "{progress}": progress,
                    "{points_awarded}": points_awarded,
                    "{total_points}": points_total,
                    "{next_tier}": next_tier,
                    "{task_tier}": task_tier,
                    "{next_tier_points}": next_tier_points,
                    "{points_left}": points_left
                }
            value_dict.update(self._group_points_placeholder_map(data))
            value_dict.update(self._plugin_version_placeholder_map(data))
            video_url = self._maybe_get_video_url(db_session, data)
            value_dict["{video_url}"] = video_url or ""
            value_dict["{video_link}"] = f"[Video]({video_url})" if video_url else ""
            # Prefer video for display; keep screenshot in data["image_url"] for attachments
            value_dict["{image_url}"] = video_url or (data.get("image_url") or "")
            
            embed = replace_placeholders(embed_template, value_dict)
            embed = self._finalize_group_points_embed(embed)
            
            # Send message
            formatted_name = get_formatted_name(player_name, group_id, db_session)
            content = f"{formatted_name} has completed a combat achievement!"
            
            if image_url:
                try:
                    local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                    if os.path.exists(local_path):
                        attachment = interactions.File(local_path)
                        message = await channel.send(content, embed=embed, files=attachment)
                    else:
                        #print(f"Debug - CA image file not found at: {local_path}")
                        message = await channel.send(content, embed=embed)
                except Exception as e:
                    #print(f"Debug - Error loading CA attachment: {e}")
                    message = await channel.send(content, embed=embed)
            else:
                message = await channel.send(content, embed=embed)
            # Prefer attaching MP4 if available (Discord renders as native video)
            if video_url:
                video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)
                if video_attachment:
                    try:
                        message = await channel.send(content, embed=embed, files=video_attachment)
                    finally:
                        if video_local_path:
                            try:
                                os.remove(video_local_path)
                            except Exception:
                                pass
            
            await self._cleanup_processed_local_video_after_send(db_session, data)
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def send_clog_notification_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Send a collection log notification to Discord with session"""
        from db.models import NotifiedSubmission
        try:
            group_id = notification.group_id
            player_id = notification.player_id
            #print(f"Found a collection log notification to send in {group_id}")
            
            # Get channel ID for this group
            channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_clog'
            ).first()
            #print(f"Found a channel id config for {group_id}")
            if not channel_id_config or not channel_id_config.config_value:
                notification.status = 'failed'
                notification.error_message = f"No channel configured for group {group_id}"
                db_session.commit()
                return
            
            clog_id = data.get('clog_id', data.get('log_id', None))
            if clog_id:
        
                existing_notification = db_session.query(NotifiedSubmission).filter(
                    NotifiedSubmission.player_id == player_id,
                    NotifiedSubmission.group_id == group_id,
                    NotifiedSubmission.clog_id == clog_id
                ).first()
                if existing_notification:
                    print(f"Drop was already notified... Skipping")
                    return
            channel = None
            channel_error = None
            channel_id = channel_id_config.config_value
            if channel_id and channel_id != "" and len(str(channel_id)) > 10:
                channel, channel_error = await self._fetch_sendable_channel(channel_id)
            else:
                channel_id_config = db_session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == 'channel_id_to_post_loot'
                ).first()
                if channel_id_config:
                    channel_id = channel_id_config.config_value
                    channel, channel_error = await self._fetch_sendable_channel(channel_id)
                else:
                    #print(f"Invalid channel id: {channel_id}")
                    notification.status = 'failed'
                    notification.error_message = f"Invalid channel id: {channel_id}"
                    db_session.commit()
                    return
            if not channel:
                #print(f"Channel not found for group {group_id} (id was passed as {channel_id})")
                notification.status = 'failed'
                notification.error_message = channel_error or f"Channel not found for group {group_id}"
                db_session.commit()
                return
            # Get data
            player_name = data.get('player_name')
            item_name = data.get('item_name')
            collection_name = data.get('collection_name')
            image_url = data.get('image_url')
            item_id = data.get('item_id')
            kc = data.get('kc_received')
            npc_name = data.get('npc_name')
            partition = get_current_partition()
            month_total_int = self._get_player_month_total(player_id, partition)
            player_month_total = format_number(month_total_int)
            
            # Get embed template
            upgrade_active = has_custom_embeds(group_id)
            if upgrade_active:
                embed_template = await self.db_ops.get_group_embed('clog', group_id)
            else:
                embed_template = await self.db_ops.get_group_embed('clog', 1)
            
            if group_id == 2:
                embed_template = await self.remove_group_field(embed_template)

            user_count = format_number(redis_client.client.zcard(f"leaderboard:{partition}:group:{group_id}"))
            # Replace placeholders
            replacements = {
                "{player_name}": f"[{player_name}](https://www.droptracker.io/players/{quote(player_name, safe='')}.{player_id}/view)",
                "{player_loot_month}": player_month_total,
                "{kc_received}": kc,
                "{item_name}": item_name,
                "{collection_name}": collection_name,
                "{item_id}": item_id,
                "{npc_name}": npc_name,
                "{total_tracked}": user_count
            }
            replacements.update(self._group_points_placeholder_map(data))
            replacements.update(self._plugin_version_placeholder_map(data))
            video_url = self._maybe_get_video_url(db_session, data)
            replacements["{video_url}"] = video_url or ""
            replacements["{video_link}"] = f"[Video]({video_url})" if video_url else ""
            # Prefer video for display; keep screenshot in data["image_url"] for attachments
            replacements["{image_url}"] = video_url or (data.get("image_url") or "")
            
            embed = replace_placeholders(embed_template, replacements)
            embed = self._finalize_group_points_embed(embed)
            
            # Send message
            formatted_name = get_formatted_name(player_name, group_id, db_session)
            content = f"{formatted_name} has added an item to their collection log!"
            
            video_attachment, video_local_path = (None, None)
            if video_url:
                video_attachment, video_local_path = await self._download_video_attachment(video_url, notification.id)

            try:
                if video_attachment:
                    message = await channel.send(content, embed=embed, files=video_attachment)
                elif image_url:
                    try:
                        local_path = image_url.replace("https://www.droptracker.io/img/", "/store/droptracker/disc/static/assets/img/")
                        if os.path.exists(local_path):
                            attachment = interactions.File(local_path)
                            message = await channel.send(content, embed=embed, files=attachment)
                        else:
                            print(f"Debug - Collection log image file not found at: {local_path}")
                            message = await channel.send(content, embed=embed)
                    except Exception as e:
                        print(f"Debug - Error loading collection log attachment: {e}")
                        message = await channel.send(content, embed=embed)
                else:
                    message = await channel.send(content, embed=embed)
            finally:
                if video_local_path:
                    try:
                        os.remove(video_local_path)
                    except Exception:
                        pass
            
            await self._cleanup_processed_local_video_after_send(db_session, data)
            notification.status = 'sent'
            notification.processed_at = datetime.now()
            db_session.commit()
            
        except Exception as e:
            notification.status = 'failed'
            notification.error_message = str(e)
            db_session.commit()
            raise

    async def remove_group_field(self, embed: interactions.Embed):
        """Removes the Group field from the embed"""
        if embed.fields:
            embed.fields = [field for field in embed.fields if "Group" not in field.name]
        return embed
    
    async def remove_kc_field(self, embed: interactions.Embed):
        """Removes the Kills field from the embed"""
        if embed.fields:
            embed.fields = [field for field in embed.fields if "Source:" not in field.name]
        return embed
    
    async def _is_not_sent_with_session(self, notification: NotificationQueue, data: dict, db_session):
        """Check if a notification has already been sent by querying the database with a specific session.
        Returns True if the notification should be sent, False if it should be skipped."""
        try:
            from db.models import NotifiedSubmission
            
            # Get the appropriate ID field based on notification type
            id_key = None
            if notification.notification_type == 'drop':
                id_key = 'drop_id'
            elif notification.notification_type == 'pb':
                id_key = 'pb_id'
            elif notification.notification_type == 'ca':
                id_key = 'ca_id'
            elif notification.notification_type == 'clog':
                id_key = 'clog_id'
            else:
                return True  # Allow other notification types to proceed
                
            if not id_key:
                return True
                
            # Get the ID to check
            notification_id = data.get(id_key)
            if not notification_id:
                return True
                
            # Check if this notification has already been sent by querying NotifiedSubmission
            existing_notification = db_session.query(NotifiedSubmission).filter(
                NotifiedSubmission.player_id == notification.player_id,
                NotifiedSubmission.group_id == notification.group_id,
                getattr(NotifiedSubmission, id_key) == notification_id
            ).first()
            
            if existing_notification:
                app_logger.log(log_type="info", 
                             data=f"Notification {notification.id} was already sent for {id_key} {notification_id} in group {notification.group_id}", 
                             app_name="notification_service", 
                             description="_is_not_sent")
                return False  # Return False to prevent sending
            
            return True  # Allow sending
            
        except Exception as e:
            app_logger.log(log_type="error", 
                         data=f"Error checking if notification was sent: {e}", 
                         app_name="notification_service", 
                         description="_is_not_sent")
            return True  # On error, allow sending to be safe

    async def cleanup_tracking_dicts(self):
        """Clean up old NotifiedSubmission entries to prevent database bloat"""
        try:
            from api.core import get_db_session, NotifiedSubmission
            with get_db_session() as db_session:
                # Delete NotifiedSubmission entries older than 30 days
                cutoff_date = datetime.now() - timedelta(days=30)
                old_entries = db_session.query(NotifiedSubmission).filter(
                    NotifiedSubmission.created_at < cutoff_date
                ).all()
                
                if old_entries:
                    for entry in old_entries:
                        db_session.delete(entry)
                    db_session.commit()
                    app_logger.log(log_type="info", 
                                 data=f"Cleaned up {len(old_entries)} old notification entries", 
                                 app_name="notification_service", 
                                 description="cleanup_tracking_dicts")
                
        except Exception as e:
            app_logger.log(log_type="error", 
                         data=f"Error cleaning up old notification entries: {e}", 
                         app_name="notification_service", 
                         description="cleanup_tracking_dicts")

    async def cleanup_stuck_notifications(self):
        """Reset notifications that have been stuck in 'processing' status for too long"""
        try:
            # Find notifications stuck in processing for more than 10 minutes
            stuck_time = datetime.now() - timedelta(minutes=10)
            stuck_notifications = session.query(NotificationQueue).filter(
                NotificationQueue.status == 'processing',
                NotificationQueue.processed_at.is_(None)
            ).all()
            
            if stuck_notifications:
                app_logger.log(log_type="warning", 
                             data=f"Found {len(stuck_notifications)} stuck notifications, resetting to pending", 
                             app_name="notification_service", 
                             description="cleanup_stuck_notifications")
                
                for notification in stuck_notifications:
                    notification.status = 'pending'
                    notification.error_message = 'Reset due to timeout'
                
                session.commit()
                
        except Exception as e:
            app_logger.log(log_type="error", 
                         data=f"Error cleaning up stuck notifications: {e}", 
                         app_name="notification_service", 
                         description="cleanup_stuck_notifications")

    def _get_player_month_total(self, player_id: int, partition: int = None) -> int:
        """Fetch the player's monthly total loot from Redis computed by redis_updates."""
        try:
            if partition is None:
                partition = get_current_partition()
            key = f"player:{player_id}:{partition}:total_loot"
            total_str = redis_client.get(key)
            if total_str is None:
                # Fallback to global leaderboard score if key missing
                score = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
                return int(float(score)) if score is not None else 0
            return int(float(total_str))
        except Exception:
            return 0

""" Helper function to get a list of registered users who are members of a specific group """
async def get_authorized_users(group_id):
    from api.core import get_db_session
    users = []
    db_session = get_db_session()
    try:
        group_config = db_session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id).all()
        # Long lists spill from config_value into long_value — read both.
        config = {
            conf.config_key: (conf.config_value or getattr(conf, "long_value", None))
            for conf in group_config
        }
        if config.get("authed_users"):
            authed_users = config["authed_users"]
            if isinstance(authed_users, int):
                authed_users = f"{authed_users}"  # Get the list of authorized user IDs
            try:
                authed_users = json.loads(authed_users)
            except (TypeError, ValueError):
                authed_users = []
            for authed_id in authed_users:
                user = db_session.query(User).filter(User.discord_id == authed_id).first()
                if user:
                    users.append(user)
    finally:
        try:
            db_session.close()
        except Exception:
            pass
    return users