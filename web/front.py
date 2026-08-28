# api.py

from datetime import datetime, timedelta
import asyncio
import json
import os
import re
import shutil
from types import TracebackType
import interactions
import markdown
from utils.format import get_sorted_doc_files, convert_from_ms, parse_authed_users, human_readable_time_difference
from db.models import Session, NpcList

from quart import Blueprint, Response, jsonify, redirect, render_template, request, session as sesh, send_from_directory, url_for

from utils import item_images
from quart_jwt_extended import (
    JWTManager,
    jwt_required,
    create_access_token,
    get_jwt_identity,
    decode_token
)
from db.ops import DatabaseOperations
from db.models import CollectionLogEntry, CombatAchievementEntry, Drop, Group, GroupConfiguration, GroupPatreon, ItemList, NotifiedSubmission, NpcList, PersonalBestEntry, Player, UserConfiguration, session as db_sesh, User, Guild

missing_file_log_path = "missing_images.json"

DOCS_FOLDER = os.path.join(os.getcwd(), 'templates/docs')

def create_frontend(bot: interactions.Client):
# Create a Blueprint object
    front = Blueprint('frontend', __name__)

    db = DatabaseOperations()
    # Define path to docs folder

    @front.route('/')
    async def homepage():
        
        user = sesh.get('user', None)
        print("Session data:", dict(sesh))
        print("User:", user)
        jwt_token = sesh.get('jwt_token', None)
        print("JWT Token:", jwt_token)
        if not user:
            return await render_template('index.html',
                                     page_name="Home",
                                     current_page="home")
        else:
            return await render_template("index.html", 
                                     user=user,
                                     page_name="Home",
                                     current_page="home")
        
    # Extensions this tree may serve for the browser to RENDER. Anything else
    # is handed back as a download instead.
    _INLINE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    def _force_download(response, filename):
        """Serve a non-image as an opaque download, never as page content.

        Ticket attachments are mirrored into this tree with whatever extension
        the uploader chose, and this route is public and unauthenticated. An
        attacker could open a support ticket, attach `poc.html` (or an .svg
        carrying script), and get it served from www.droptracker.io — same
        origin as the session cookie, so the script could drive every BFF route
        as whoever opened the link. Downloading it instead keeps genuinely
        useful non-image attachments (client.log, droptracker.log) working
        while making them inert, and this covers the files ALREADY on disk, not
        just newly mirrored ones.
        """
        response.headers['Content-Type'] = 'application/octet-stream'
        response.headers['Content-Disposition'] = (
            f'attachment; filename="{os.path.basename(filename)}"'
        )
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def _missing_item_icon():
        """The response for an item icon we could not produce.

        Scoped deliberately to ``itemdb/`` and nothing else. The generic
        fallback below (the DropTracker GIF at 200) is reached by group icons,
        NPC art and every other path under this route, and several of those
        surfaces rely on getting *an* image back — changing the contract for all
        of them to fix item icons would be a much larger blast radius than the
        bug warrants.

        404 rather than 200 because the old 200 actively defeated the frontend:
        `<img>` onError never fires for a "successful" response, so a missing
        icon was indistinguishable from a real one and rendered a 672 KB logo
        instead of a 400-byte sprite. The body is still a valid (transparent,
        1x1) PNG so that consumers which ignore the status — Discord's unfurler,
        the many raw `<img>` tags that do not go through ItemDbIcon — degrade to
        something invisible rather than to a browser's broken-image glyph.
        """
        response = Response(
            item_images.TRANSPARENT_PNG, status=404, mimetype='image/png'
        )
        response.headers[item_images.PLACEHOLDER_HEADER] = 'item'
        # Uncacheable, so a failure cannot outlive its cause. The previous
        # placeholder was held at the edge for a full day after the origin had
        # already been repaired.
        response.headers['Cache-Control'] = item_images.PLACEHOLDER_CACHE_CONTROL
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    def _missing_model_image():
        """The response for a character avatar that does not exist.

        Scoped to ``models/``, and for the same reason ``_missing_item_icon``
        is scoped to ``itemdb/``: the generic fallback below serves group icons
        and NPC art, whose callers rely on getting an image back.

        This path needs it more than items do. Most players have no character
        model — that is the designed default, not a fault — so the frontend
        asks for an avatar it usually will not get, and the letter placeholder
        it falls back to is reached through the `<img>` error event. A 200 with
        a 672 KB logo does not fire that event, so every model-less player in a
        leaderboard would have rendered a full-size animated logo.

        Unlike a missing item icon, a missing avatar is not a repairable fault,
        so it is cacheable: re-asking on every page view would be pure waste.
        The TTL is short enough that a player who uploads a model sees it
        appear within minutes.
        """
        response = Response(
            item_images.TRANSPARENT_PNG, status=404, mimetype='image/png'
        )
        response.headers[item_images.PLACEHOLDER_HEADER] = 'model'
        response.headers['Cache-Control'] = 'public, max-age=600'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    # models/{player_id}/avatar.png              -> current outfit, resolved live
    # models/{player_id}/{fingerprint}-avatar.png -> one specific outfit
    _AVATAR_RE = re.compile(r'^models/(\d+)/(avatar|[0-9a-f]{1,32}-avatar)\.png$')

    async def _serve_avatar(player_id: int, which: str):
        """Torso-up avatar crop, derived from the full render on first request.

        Derived on demand rather than only at upload time so the 2600 outfits
        that predate this feature work without a backfill, and so a crop lost to
        a prune or a disk repair comes back by itself.

        The work runs in a thread: this route is served from inside the core bot
        process, and a Pillow crop of an 800x1200 PNG on the event loop would
        stall the Discord gateway — a burst of uncached avatars from one
        leaderboard view is exactly the shape of stall this repo has hit before.
        """
        from services import player_avatar

        if which == 'avatar':
            fingerprint = await asyncio.to_thread(
                player_avatar.current_fingerprint, player_id
            )
            if not fingerprint:
                return _missing_model_image()
        else:
            fingerprint = which[:-len('-avatar')]

        path = await asyncio.to_thread(
            player_avatar.ensure_avatar, player_id, fingerprint
        )
        if not path:
            return _missing_model_image()

        response = await send_from_directory(
            os.path.dirname(path), os.path.basename(path)
        )
        # `avatar.png` is a stable alias whose *content* changes when the player
        # changes gear, so it cannot take the directory default (12 hours) — an
        # outfit change would take half a day to show up. An hour bounds that
        # while keeping avatars off the origin for the length of a session.
        # (Cloudflare's zone Browser Cache TTL rewrites short max-ages upward
        # anyway, so anything under ~30 minutes is not actually honoured.)
        response.headers['Cache-Control'] = 'public, max-age=3600'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @front.route('/img/<path:filename>')
    async def serve_img(filename):
        _is_inline_image = filename.lower().endswith(_INLINE_IMAGE_EXTS)
        # Avatars are derived artifacts, so this runs BEFORE the exists() check
        # below: `avatar.png` is a stable per-player alias that never exists on
        # disk under that name, and a fingerprinted crop may not have been
        # built yet.
        _avatar = _AVATAR_RE.match(filename)
        if _avatar:
            return await _serve_avatar(int(_avatar.group(1)), _avatar.group(2))
        ## Check if the file exists
        if not os.path.exists(os.path.join('static/assets/img', filename)):
            # A missing character model or render is not a repairable fault and
            # must not answer with the logo — see _missing_model_image.
            if filename.startswith('models/'):
                return _missing_model_image()
            # Grayscale receipt-tab variants (Loot Sweep board): serve
            # itemdb/gray/{id}.png. Pre-baked files are served straight through
            # by the send_from_directory at the end of this function; this
            # branch only runs for a variant that hasn't been generated yet —
            # desaturate the colour icon once (recovering it first if missing),
            # cache it, and serve. Moves the old per-tab CSS grayscale raster
            # off every website client. Backfill: scripts/generate_grayscale_icons.py.
            if filename.startswith('itemdb/gray/') and filename.endswith('.png'):
                gid = filename[len('itemdb/gray/'):-len('.png')]
                if gid.isdigit():
                    try:
                        from utils.item_images import (
                            ensure_item_image,
                            ensure_grayscale_variant,
                            item_image_path,
                        )
                        if not os.path.exists(item_image_path(gid)):
                            await ensure_item_image(gid)
                        if ensure_grayscale_variant(gid):
                            return await send_from_directory('static/assets/img', filename)
                    except Exception as e:
                        print(f"On-demand grayscale fetch failed for {filename}: {e}")
                    # Never leave a receipt tab blank — fall back to the colour icon.
                    color_rel = f'itemdb/{gid}.png'
                    if os.path.exists(os.path.join('static/assets/img', color_rel)):
                        return await send_from_directory('static/assets/img', color_rel)
                # A receipt tab with no icon at all is an item icon we could not
                # produce, so it gets the item placeholder rather than the logo.
                return _missing_item_icon()
            # Item icons: recover a missing itemdb/{id}.png on demand from the
            # RuneLite cache, the same way the lootboard generator does. Without
            # this the website perpetually renders newly-tracked items as the
            # placeholder GIF until (if ever) a board render happens to fetch
            # the icon. Mirrors the NPC-backup recovery below.
            if filename.startswith('itemdb/') and filename.endswith('.png'):
                item_id = filename[len('itemdb/'):-len('.png')]
                try:
                    from utils.item_images import ensure_item_image
                    if await ensure_item_image(item_id):
                        return await send_from_directory('static/assets/img', filename)
                except Exception as e:
                    print(f"On-demand item icon fetch failed for {filename}: {e}")
                # The self-heal above still ran, so requesting a missing icon
                # repairs it for next time; this only answers the request that
                # discovered the gap. Repeat misses are a dict lookup, not
                # another round trip — see item_images._negative_cache.
                return _missing_item_icon()
            if ".png" in filename or ".jpeg" in filename or ".jpg" in filename or ".gif" in filename:
                # Strip the directory prefix and extension to recover the bare
                # npc id (e.g. "npcdb/12345.png" -> "12345"). Each step must
                # chain off the previous result — the original code reassigned
                # from `filename` every line, so only the last replace applied
                # and the extension was never stripped, leaving target as
                # "12345.png" which never matched an integer npc_id.
                target = filename.replace("npcdb/", "")
                target = target.replace(".png", "").replace(".jpeg", "").replace(".jpg", "").replace(".gif", "")
                # Short-lived session: this route runs inside the core bot, and
                # a read on the module-global scoped session left an idle
                # transaction open until some unrelated commit (observed live
                # 2026-07-16 growing past 2 minutes after one missing-image hit).
                with Session() as img_session:
                    npc = img_session.query(NpcList).filter(NpcList.npc_id == target).first()
                ## Add the file to the missing file log
                ## Check if the file exists in the backup path
                if npc:
                    npc_name = npc.npc_name
                    formatted_name = npc_name.replace(" ", "_").replace("'", "").replace("(","").replace(")","")
                    formatted_name = formatted_name.lower() + ".png"
                    backup_path = os.path.join('static/assets/img/npc_backup/', formatted_name)
                    if os.path.exists(backup_path):
                        ## Copy the file to the main path
                        shutil.copy(backup_path, os.path.join('static/assets/img', filename))
                        return await send_from_directory('static/assets/img/npc_backup/', formatted_name)
                    else:
                        print(f"Image not found: {filename}")
                        with open(missing_file_log_path, 'a') as f:
                            f.write(f"{filename} - {npc_name}\n")
            else:
                print(f"No .png, .jpeg, or .jpg extension found in {filename}")
            return await send_from_directory('static/assets/img', 'droptracker-small.gif')
        response = await send_from_directory('static/assets/img', filename)
        if not _is_inline_image:
            return _force_download(response, filename)
        # Even a real image gets nosniff: a .png whose bytes are HTML must not
        # be re-interpreted as a document.
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    
    @front.route('/user-upload/<path:filename>')
    async def serve_user_img(filename):
        return await send_from_directory('static/assets/img/user-upload')
  
    return front

async def get_guild(bot: interactions.Client, guild_id):
    print("get_guild called with bot:", bot, "and guild_id:", guild_id)
    print(f"Bot:", bot.user.username, "ID", bot.user.id)
    try:
        guild = await bot.fetch_guild(guild_id=guild_id)
        return guild
    except Exception as e:
        print("Couldn't get the guild with .fetch_guild:", e)
    return None
