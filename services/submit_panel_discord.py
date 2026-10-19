"""The in-Discord Submit button (suggestion #198): cards, flow and modal.

Members tap **Submit** on the Activity launcher card (``submit_on_launch_card``)
or on a standalone Submit card (``submit_panel_channel``), pick what they are
submitting, and fill in a modal with an optional screenshot. The submission
goes through the same checks and the same intake endpoint as ``/submit``
(``commands/submissions.py``): only accounts the member has claimed, the shared
per-user rate limit, every group's manual-submission policy, and the events
engine. Members who have not linked an account are pointed at the website.

The modal is sent and answered through raw interaction calls because it uses
Discord's Label + File Upload modal components, which the bundled
interactions.py does not model. :func:`_patch_modal_context` keeps the
library's own modal parser from raising on those components (it would
otherwise log a traceback for every submit). Pure pieces live in
``services/submit_panel_core.py``.
"""
from __future__ import annotations

import functools
import logging

import httpx
from interactions import (
    ActionRow,
    Button,
    ButtonStyle,
    Extension,
    StringSelectMenu,
    StringSelectOption,
    listen,
)
from interactions.api.events import Component, RawGatewayEvent

from services import submit_panel_core as core

log = logging.getLogger("submit_panel")

_CALLBACK_MODAL = 9
_CALLBACK_DEFERRED_MESSAGE = 5
_FLAG_EPHEMERAL = 64
_INTERACTION_MODAL_SUBMIT = 5

_COLOR_SUCCESS = 0x2ECC71
_COLOR_ERROR = 0xE74C3C

#: Flipped off for the life of the process if Discord ever refuses the File
#: Upload component, so every later modal goes straight to the plain version.
_upload_supported = True

_LINK_HINT = (
    "Link your RuneScape account first: sign in at https://www.droptracker.io and "
    "claim it, or use `/claim-rsn`. Submissions only count for accounts you own."
)


# ── cards ────────────────────────────────────────────────────────────────────

def submit_button() -> Button:
    return Button(style=ButtonStyle.GREEN, label=core.SUBMIT_BUTTON_LABEL,
                  custom_id=core.SUBMIT_BUTTON_ID)


def build_panel_card(message):
    """The standalone Submit card (Components V2)."""
    from interactions.models import (
        ContainerComponent,
        SeparatorComponent,
        TextDisplayComponent,
    )

    return [
        ContainerComponent(
            TextDisplayComponent(content="## Submit an entry"),
            TextDisplayComponent(content=core.panel_text(message)),
            SeparatorComponent(divider=True),
            ActionRow(submit_button()),
            TextDisplayComponent(
                content="-# Powered by the [DropTracker](https://www.droptracker.io)"),
        )
    ]


async def _post_panel(bot, message, channel_id, variant=None):
    try:
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel is None:
            return None
        return await channel.send(components=build_panel_card(message))
    except Exception:
        log.warning("[SubmitPanel] couldn't post card to channel %s", channel_id, exc_info=True)
        return None


async def _edit_panel(bot, message, channel_id, message_id, variant) -> bool:
    try:
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel is None:
            return False
        posted = await channel.fetch_message(message_id=message_id)
        if posted is None:
            return False
        await posted.edit(components=build_panel_card(message))
        return True
    except Exception:
        log.warning("[SubmitPanel] couldn't edit card %s/%s", channel_id, message_id, exc_info=True)
        return False


async def _delete_message(bot, channel_id, message_id) -> None:
    try:
        channel = await bot.fetch_channel(channel_id=channel_id)
        if channel is None:
            return
        old = await channel.fetch_message(message_id=message_id)
        await old.delete()
    except Exception:
        log.debug("[SubmitPanel] couldn't delete stale card %s/%s", channel_id, message_id,
                  exc_info=True)


async def reconcile_all(bot, session_factory) -> None:
    """Keep each group's standalone Submit card in step with its settings:
    post it, move it, edit it when the message changes, remove it when the
    channel is cleared. DB reads only in steady state."""
    from db.models import GroupConfiguration
    from services.activity_launch_core import clean_channel, reconcile_group

    session = session_factory()
    try:
        rows = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.config_key.in_((
                core.PANEL_CHANNEL_KEY, core.PANEL_REF_KEY,
                core.PANEL_VARIANT_KEY, core.PANEL_MESSAGE_KEY)))
            .all()
        )
        channels, refs, variants, messages = {}, {}, {}, {}
        for r in rows:
            if r.config_key == core.PANEL_CHANNEL_KEY:
                channels[r.group_id] = clean_channel(r.config_value)
            elif r.config_key == core.PANEL_REF_KEY:
                refs[r.group_id] = r
            elif r.config_key == core.PANEL_VARIANT_KEY:
                variants[r.group_id] = r
            else:
                messages[r.group_id] = r.config_value
        for group_id in set(channels) | set(refs):
            message = messages.get(group_id)
            try:
                await reconcile_group(
                    session, group_id, channels.get(group_id), refs.get(group_id),
                    post_card=functools.partial(_post_panel, bot, message),
                    delete_message=functools.partial(_delete_message, bot),
                    variant=core.panel_variant(message),
                    variant_row=variants.get(group_id),
                    edit_card=functools.partial(_edit_panel, bot, message),
                    ref_key=core.PANEL_REF_KEY,
                    variant_key=core.PANEL_VARIANT_KEY,
                )
            except Exception:
                log.exception("[SubmitPanel] reconcile failed for group %s", group_id)
    finally:
        session.close()


# ── the modal parser patch ───────────────────────────────────────────────────

def _patch_modal_context() -> None:
    """Make interactions.py's ModalContext tolerate Label-wrapped components.

    Only Label-shaped payloads are rewritten, so every existing modal
    (entry_modifier, onboarding) parses exactly as before.
    """
    try:
        from interactions.models.internal.context import ModalContext
    except Exception:
        return
    if getattr(ModalContext, "_dt_label_patch", False):
        return
    original = ModalContext.from_dict.__func__

    def from_dict(cls, client, payload):
        # Flatten up front rather than retrying after a failure: the first
        # attempt pops ``member["user"]`` from the payload, so a retry on the
        # same dict always died with KeyError 'user' (ticket #489).
        data = payload.get("data") or {}
        if core.has_label_components(data.get("components")):
            data = {**data, "components": core.flatten_modal_components(data.get("components"))}
            payload = {**payload, "data": data}
        return original(cls, client, payload)

    ModalContext.from_dict = classmethod(from_dict)
    ModalContext._dt_label_patch = True


# ── the flow ─────────────────────────────────────────────────────────────────

def _embed(title: str, description: str, color: int) -> dict:
    return {"title": title, "description": description[:4000], "color": color}


def _canonical_name(sql, raw):
    """Snap a typed name to the catalog spelling the website search would pick:
    an exact (case-insensitive) match, else the single best tracked match.
    Unknown names go through untouched and the intake explains the problem."""
    if not raw:
        return raw
    try:
        from db.models import session

        rows = session.execute(sql, {"pat": f"%{raw}%"}).fetchall()
    except Exception:
        return raw
    names = [r[0] for r in rows]
    for name in names:
        if name.lower() == raw.lower():
            return name
    prefixed = [n for n in names if n.lower().startswith(raw.lower())]
    if len(prefixed) == 1:
        return prefixed[0]
    return raw


async def _fetch_attachment(attachment: dict):
    """``(filename, bytes, content_type)`` for an uploaded screenshot, or a
    string explaining why it can't be used."""
    from data.submissions.manual_proof import PROOF_CONTENT_TYPES, PROOF_MAX_BYTES

    content_type = str(attachment.get("content_type") or "").split(";")[0].strip().lower()
    if content_type not in PROOF_CONTENT_TYPES:
        return "The screenshot must be a PNG, JPEG, WebP or GIF image."
    if int(attachment.get("size") or 0) > PROOF_MAX_BYTES:
        return "Screenshots are capped at 10 MB."
    url = attachment.get("url") or attachment.get("proxy_url")
    if not url:
        return "Couldn't read the screenshot from Discord."
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception:
        return "Couldn't download the screenshot from Discord."
    return (attachment.get("filename") or "proof.png", resp.content, content_type)


class SubmitPanel(Extension):
    def __init__(self, bot):
        self.bot = bot
        _patch_modal_context()

    @listen(Component)
    async def on_submit_component(self, event: Component):
        ctx = event.ctx
        custom_id = getattr(ctx, "custom_id", "") or ""
        try:
            if custom_id == core.SUBMIT_BUTTON_ID:
                await self._open(ctx)
            elif custom_id == core.TYPE_SELECT_ID:
                await self._open_modal(ctx)
        except Exception:
            log.exception("[SubmitPanel] component failed: %s", custom_id)
            try:
                await ctx.send("Something went wrong. Please try again.", ephemeral=True)
            except Exception:
                pass

    async def _open(self, ctx):
        from commands.submissions import claimed_players
        from db.models import session

        session.expire_all()
        players, err = claimed_players(ctx.author.id)
        if err:
            await ctx.send(f"{err}\n\n{_LINK_HINT}", ephemeral=True)
            return
        menu = StringSelectMenu(
            *[StringSelectOption(label=label, value=key, description=desc)
              for key, label, desc in core.TYPE_CHOICES],
            custom_id=core.TYPE_SELECT_ID,
            placeholder="What are you submitting?",
        )
        await ctx.send("What are you submitting?", components=[ActionRow(menu)], ephemeral=True)

    async def _open_modal(self, ctx):
        """Answer the type pick with the modal. Must be the initial response."""
        global _upload_supported
        from commands.submissions import claimed_players
        from db.models import session

        values = list(getattr(ctx, "values", None) or [])
        sub_type = values[0] if values else None
        if sub_type not in core.TYPE_LABELS:
            await ctx.send("Pick what you are submitting.", ephemeral=True)
            return
        session.expire_all()
        players, err = claimed_players(ctx.author.id)
        if err:
            await ctx.send(f"{err}\n\n{_LINK_HINT}", ephemeral=True)
            return
        accounts = [p.player_name for p in players]
        attempts = [True, False] if _upload_supported else [False]
        for with_upload in attempts:
            try:
                await self.bot.http.post_initial_response(
                    {"type": _CALLBACK_MODAL,
                     "data": core.build_modal(sub_type, accounts, with_upload=with_upload)},
                    ctx.id, ctx.token)
                ctx.responded = True
                return
            except Exception:
                if with_upload:
                    # Discord refused the File Upload component: remember and
                    # offer the plain modal (screenshot via /submit proof).
                    _upload_supported = False
                    log.warning("[SubmitPanel] modal with file upload refused; "
                                "falling back to the plain modal", exc_info=True)
                    continue
                raise

    @listen("raw_interaction_create")
    async def on_raw_modal(self, event: RawGatewayEvent) -> None:
        data = event.data or {}
        if data.get("type") != _INTERACTION_MODAL_SUBMIT:
            return
        sub_type = core.modal_type((data.get("data") or {}).get("custom_id"))
        if sub_type is None:
            return
        try:
            await self._modal_submit(data, sub_type)
        except Exception:
            log.exception("[SubmitPanel] modal submit failed")

    async def _modal_submit(self, data: dict, sub_type: str) -> None:
        interaction_id, token = data.get("id"), data.get("token")
        if not interaction_id or not token:
            return
        await self.bot.http.post_initial_response(
            {"type": _CALLBACK_DEFERRED_MESSAGE, "data": {"flags": _FLAG_EPHEMERAL}},
            interaction_id, token)

        async def reply(title, text, color):
            try:
                await self.bot.http.edit_interaction_message(
                    {"embeds": [_embed(title, text, color)]}, self.bot.app.id, token)
            except Exception:
                log.exception("[SubmitPanel] couldn't edit the deferred reply")

        discord_id = core.interaction_user_id(data)
        if not discord_id:
            log.warning("[SubmitPanel] modal submit %s carried no user id", interaction_id)
            await reply("Couldn't submit that", "Something went wrong. Please try again.",
                        _COLOR_ERROR)
            return
        try:
            result = await self._process(sub_type, data.get("data") or {}, discord_id)
        except Exception:
            log.exception("[SubmitPanel] modal submit failed")
            result = ("Couldn't submit that", "Something went wrong. Please try again.",
                      _COLOR_ERROR)
        await reply(*result)

    async def _process(self, sub_type: str, modal_data: dict, discord_id):
        """``(title, text, color)`` for the member's reply."""
        from commands.submissions import (
            _ITEM_AC_SQL,
            _NPC_AC_SQL,
            claimed_players,
            forward_to_intake,
            policy_notices,
            rate_limited,
        )
        from data.submissions.manual_discord import (
            build_manual_payload,
            parse_kill_time_ms,
            summarize_submission,
        )
        from db.models import session
        from utils.format import pick_player_by_rsn

        failed = "Couldn't submit that"
        values, attachments = core.parse_modal_submit(modal_data)
        session.expire_all()
        players, err = claimed_players(discord_id)
        if err:
            return failed, f"{err}\n\n{_LINK_HINT}", _COLOR_ERROR
        account = (values.get(core.ACCOUNT_FIELD) or "").strip()
        if account:
            player = pick_player_by_rsn(players, account)
            if player is None:
                return failed, f"`{account}` isn't one of your claimed accounts.", _COLOR_ERROR
        elif len(players) == 1:
            player = players[0]
        else:
            return failed, "Pick which of your accounts this is for.", _COLOR_ERROR
        if rate_limited(player.user_id):
            return failed, "You're submitting too fast. Wait a minute and try again.", _COLOR_ERROR
        try:
            fields = core.payload_fields(sub_type, values, parse_kill_time_ms)
            if fields.get("item_name"):
                fields["item_name"] = _canonical_name(_ITEM_AC_SQL, fields["item_name"])
            if fields.get("npc_name"):
                fields["npc_name"] = _canonical_name(_NPC_AC_SQL, fields["npc_name"])
            payload = build_manual_payload(sub_type, player.player_name, **fields)
        except ValueError as e:
            return failed, str(e), _COLOR_ERROR

        image = None
        if attachments:
            fetched = await _fetch_attachment(attachments[0])
            if isinstance(fetched, str):
                return failed, fetched, _COLOR_ERROR
            image = fetched

        notices = policy_notices(player)
        ok, result = await forward_to_intake(payload, image)
        if not ok:
            return "Submission not accepted", result, _COLOR_ERROR

        summary = summarize_submission(sub_type, payload)
        lines = [f"{summary} for `{player.player_name}`."]
        message = (result.get("message") or "").strip()
        if message:
            lines.append(message)
        notice = (result.get("notice") or "").strip()
        if notice:
            lines.append(f"⚠️ {notice}")
        if image is None:
            lines.append("-# No screenshot attached. If staff ask for one, add it "
                         "with `/submit proof`.")
        if notices:
            lines += ["", "**Heads up about your groups:**", *notices]
        return f"{core.TYPE_LABELS[sub_type]} submitted", "\n".join(lines), _COLOR_SUCCESS


def setup(bot) -> None:
    SubmitPanel(bot)
