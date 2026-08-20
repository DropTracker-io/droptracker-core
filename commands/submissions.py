"""
Manual Submission Commands Module (/submit ...)

Discord-side manual submissions, mirroring the website's /submit form:

- /submit drop   — a drop you received (item + NPC, optional value/quantity)
- /submit clog   — a new collection log unlock
- /submit pb     — a new personal best kill time
- /submit ca     — a completed combat achievement task
- /submit pet    — a pet drop
- /submit proof  — attach a screenshot to a submission already awaiting review

Proof stays OPTIONAL (plenty of submissions never touch an event), but the
``proof`` option is the first optional option on every subcommand, and a
proofless submission that the events engine actually parked for admin review
gets an ephemeral nudge plus a way to attach the screenshot afterwards —
see data/submissions/manual_proof.py.

Item / NPC / boss options autocomplete against the live catalog using the
same queries as the website's search (tracked entries first, duplicate
variants collapsed). The account option autocompletes the caller's claimed
RSNs and is only required when they have more than one.

Authorization matches web_api/routes/submissions.py: you can only submit for
players your Discord account has claimed. Payloads are forwarded to the
intake API's /manual-submit endpoint (X-DT-Manual-Key shared secret), so the
full backend pipeline — item/NPC validation, GE valuation, dedupe,
high-value verification, per-group manual-submission policies, notifications
and events — applies identically to Discord and web submissions.
"""

import asyncio
import os

import httpx
from interactions import (
    Attachment,
    AutocompleteContext,
    Embed,
    Extension,
    OptionType,
    SlashCommandChoice,
    SlashContext,
    listen,
    slash_command,
    slash_option,
)
from interactions.api.events import MessageCreate
from sqlalchemy import text

from data.submissions.manual_discord import (
    CA_TIERS,
    MAX_SPLIT_SIZE,
    POLICY_NOTICE,
    POLICY_NOTICE_FALLBACK,
    build_manual_payload,
    parse_kill_time_ms,
    payload_to_form,
    summarize_submission,
)
from data.submissions.manual_proof import (
    PROOF_ATTACH_LOOKBACK_SECONDS,
    PROOF_CONTENT_TYPES,
    PROOF_MAX_BYTES,
    PROOF_POLL_DELAYS,
    PROOF_PROMPT_LOOKBACK_SECONDS,
    attach_proof_url,
    attached_summary_text,
    clear_awaiting_proof,
    events_active,
    latest_batch,
    load_awaiting_proof,
    pending_proof_rows,
    proof_attachment_error,
    proof_extension,
    proof_prompt_text,
    stash_awaiting_proof,
)
from db.models import Player, User, session
from utils.download import download_player_image
from utils.format import get_command_id
from utils.npc_names import npc_match_key
from utils.redis import redis_client
from utils.submission_messages import friendly_rejection

INTAKE_API_URL = os.getenv("INTAKE_API_URL", "http://127.0.0.1:31323")
MANUAL_SUBMIT_KEY_HEADER = "X-DT-Manual-Key"
INTAKE_TIMEOUT_SECONDS = 45.0

# Shares the website's per-user limit AND Redis key, so a user can't double
# their throughput by alternating surfaces (web_api/routes/submissions.py).
_RATE_LIMIT_PER_MIN = int(os.getenv("WEB_MANUAL_SUBMIT_PER_MIN", "20"))

_COLOR_SUCCESS = 0x2ECC71
_COLOR_REJECTED = 0xE67E22
_COLOR_ERROR = 0xE74C3C
_COLOR_PROOF_PROMPT = 0x3498DB

_TYPE_LABELS = {
    "drop": "Drop",
    "clog": "Collection log",
    "pb": "Personal best",
    "ca": "Combat achievement",
    "pet": "Pet",
}

# Same catalog queries as the website search (web_api/routes/search.py):
# rank ids that have actually been received (hourly-totals presence) first so
# the real item/NPC outranks cosmetic/duplicate catalog variants, then
# collapse duplicates. Autocomplete shows at most 25 choices.
_ITEM_AC_SQL = text(
    "SELECT i.item_name, "
    "       EXISTS(SELECT 1 FROM player_item_hourly_totals t "
    "              WHERE t.item_id = i.item_id) AS tracked "
    "FROM items i WHERE i.item_name LIKE :pat AND i.noted = 0 "
    "ORDER BY tracked DESC, i.item_name ASC, i.item_id ASC LIMIT 100"
)
_NPC_AC_SQL = text(
    "SELECT n.npc_name, "
    "       EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
    "              WHERE t.npc_id = n.npc_id) AS tracked "
    "FROM npc_list n WHERE n.npc_name LIKE :pat "
    "ORDER BY tracked DESC, n.npc_name ASC, n.npc_id ASC LIMIT 100"
)


def _account_option(required: bool = False):
    return slash_option(
        name="account",
        description="Which of your claimed accounts this is for (optional if you only have one)",
        opt_type=OptionType.STRING,
        required=required,
        autocomplete=True,
        max_length=20,
    )


def _proof_option():
    """Optional everywhere, but declared directly under the last REQUIRED option
    of each subcommand: interactions.py builds the option list by inserting each
    decorator at index 0, so the top-to-bottom decorator order is what Discord's
    picker shows — and an optional option may not precede a required one."""
    return slash_option(
        name="proof",
        description="Screenshot of it — needed if this counts towards an event (PNG/JPEG/WebP/GIF, 10 MB)",
        opt_type=OptionType.ATTACHMENT,
        required=False,
    )


class SubmissionCommands(Extension):
    """Extension providing the /submit manual-submission commands."""

    def __init__(self, bot):
        self.bot = bot

    def _refresh_session(self):
        """Reset scoped session state before handling a new interaction."""
        session.remove()

    # ------------------------------------------------------------------
    # Shared plumbing
    # ------------------------------------------------------------------

    def _claimed_players(self, discord_id):
        """(players, error_message): every RSN this Discord account has claimed."""
        user = session.query(User).filter(User.discord_id == str(discord_id)).first()
        if user is None:
            return [], (
                "You don't have a DropTracker account yet. Claim your RuneScape "
                "account with `/claim-rsn` first, then try again."
            )
        players = (
            session.query(Player)
            .filter(Player.user_id == user.user_id)
            .order_by(Player.player_name)
            .all()
        )
        if not players:
            return [], (
                "You haven't claimed any RuneScape accounts yet. Claim one with "
                "`/claim-rsn` first — manual submissions only count for accounts you own."
            )
        return players, None

    def _resolve_player(self, ctx: SlashContext, account: str | None):
        """(player, error_message): the claimed player this submission is for.

        Mirrors the web rule: you can only submit for accounts you own. With
        one claimed account it's implicit; with several the account option
        (autocompleted) picks one.
        """
        players, err = self._claimed_players(ctx.author.id)
        if err:
            return None, err
        if account:
            wanted = account.strip().lower()
            for p in players:
                if (p.player_name or "").strip().lower() == wanted:
                    return p, None
            names = ", ".join(f"`{p.player_name}`" for p in players[:10])
            return None, (
                f"`{account}` isn't one of your claimed accounts. Yours: {names}."
            )
        if len(players) == 1:
            return players[0], None
        names = ", ".join(f"`{p.player_name}`" for p in players[:10])
        return None, (
            f"You have multiple claimed accounts ({names}) — pick one with the "
            "`account` option."
        )

    def _rate_limited(self, user_id: int) -> bool:
        conn = getattr(redis_client, "client", None)
        if conn is None:
            return False
        try:
            key = f"web:ratelimit:manual:{user_id}"
            count = conn.incr(key)
            if count == 1:
                conn.expire(key, 60)
            return count > _RATE_LIMIT_PER_MIN
        except Exception:
            return False

    def _policy_notices(self, player) -> list[str]:
        """Per-group heads-up lines for groups that hold/exclude this player's
        manual submissions (same preflight the web submit page shows)."""
        try:
            from data.submissions.manual_policy import manual_moderation_for_player

            groups = player.groups or []
            gid_name = {g.group_id: g.group_name for g in groups}
            moderation = manual_moderation_for_player(session, player, list(gid_name))
            lines = [
                f"**{gid_name.get(gid, f'Group {gid}')}**: this submission "
                f"{POLICY_NOTICE.get(policy, POLICY_NOTICE_FALLBACK)}."
                for gid, (_status, policy) in sorted(
                    moderation.items(),
                    key=lambda kv: (gid_name.get(kv[0]) or "").lower(),
                )
            ]
            return lines
        except Exception:
            # The heads-up is best-effort; never block a submission on it.
            return []

    async def _fetch_proof(self, proof: Attachment):
        """(bytes | None, content_type, filename, error | warning).

        A bad attachment type/size is a hard error (4th element set, bytes
        None); a failed CDN download degrades to submitting without proof
        (warning text returned alongside None bytes).
        """
        content_type = (proof.content_type or "").split(";")[0].strip().lower()
        if content_type not in PROOF_CONTENT_TYPES:
            return None, None, None, (
                "That proof file type isn't supported — attach a PNG, JPEG, WebP or GIF image."
            )
        if (proof.size or 0) > PROOF_MAX_BYTES:
            return None, None, None, "Proof screenshots are capped at 10 MB."
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(proof.url)
                resp.raise_for_status()
                return resp.content, content_type, proof.filename or "proof.png", None
        except Exception:
            return None, None, None, None  # degrade: submit without proof

    async def _forward_to_intake(self, payload: dict, image: tuple | None):
        """POST to the intake /manual-submit endpoint. Returns (ok, data|str)."""
        key = (os.getenv("MANUAL_SUBMIT_KEY") or "").strip()
        if not key:
            return False, "Manual submissions aren't configured on the server right now."
        headers = {MANUAL_SUBMIT_KEY_HEADER: key}
        url = f"{INTAKE_API_URL}/manual-submit"
        try:
            async with httpx.AsyncClient(timeout=INTAKE_TIMEOUT_SECONDS) as client:
                if image is not None:
                    filename, content, content_type = image
                    resp = await client.post(
                        url,
                        data=payload_to_form(payload),
                        files={"image_file": (filename, content, content_type)},
                        headers=headers,
                    )
                else:
                    resp = await client.post(url, json=payload, headers=headers)
        except Exception:
            return False, "The submission pipeline is unavailable right now — try again shortly."
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code >= 400 or data.get("success") is False:
            # Same wording the website shows for the same rejection
            # (utils/submission_messages.py) — the pipeline's own messages are
            # written for logs, not for the person who submitted.
            return False, friendly_rejection(
                data,
                fallback=f"The submission service returned an error ({resp.status_code}).",
            )
        return True, data

    async def _handle_submission(
        self,
        ctx: SlashContext,
        sub_type: str,
        account: str | None,
        proof: Attachment | None,
        **fields,
    ):
        await ctx.defer(ephemeral=True)
        self._refresh_session()

        player, err = self._resolve_player(ctx, account)
        if err:
            return await ctx.send(embed=self._error_embed(err), ephemeral=True)

        if self._rate_limited(player.user_id):
            return await ctx.send(
                embed=self._error_embed("You're submitting too fast — wait a minute and try again."),
                ephemeral=True,
            )

        try:
            payload = build_manual_payload(sub_type, player.player_name, **fields)
        except ValueError as e:
            return await ctx.send(embed=self._error_embed(str(e)), ephemeral=True)

        image = None
        proof_warning = None
        if proof is not None:
            content, content_type, filename, err = await self._fetch_proof(proof)
            if err:
                return await ctx.send(embed=self._error_embed(err), ephemeral=True)
            if content is not None:
                image = (filename, content, content_type)
            else:
                proof_warning = (
                    "Couldn't fetch your attachment from Discord — the submission "
                    "was sent **without** proof."
                )

        policy_lines = self._policy_notices(player)

        ok, result = await self._forward_to_intake(payload, image)
        if not ok:
            return await ctx.send(
                embed=self._error_embed(result, title="Submission not accepted"),
                ephemeral=True,
            )

        summary = summarize_submission(sub_type, payload)
        lines = [f"{summary} for `{player.player_name}`."]
        message = (result.get("message") or "").strip()
        if message:
            lines.append(message)
        notice = (result.get("notice") or "").strip()
        if notice:
            lines.append(f"⚠️ {notice}")
        if proof_warning:
            lines.append(f"⚠️ {proof_warning}")
        if policy_lines:
            lines.append("")
            lines.append("**Heads up about your groups:**")
            lines.extend(policy_lines)

        embed = Embed(
            title=f"{_TYPE_LABELS[sub_type]} submitted",
            description="\n".join(lines),
            color=_COLOR_SUCCESS,
        )
        embed.set_footer(text="Manual submission • notifications post shortly if it was accepted")
        if image is not None and proof is not None:
            embed.set_thumbnail(url=proof.url)
        sent = await ctx.send(embed=embed, ephemeral=True)
        if image is None:
            # Only nags when the events engine actually parked this submission
            # for review — a casual submission outside any event stays silent.
            await self._prompt_for_proof(ctx, player, summary)
        return sent

    def _error_embed(self, message: str, title: str = "Couldn't submit that") -> Embed:
        return Embed(title=title, description=message, color=_COLOR_ERROR)

    # ------------------------------------------------------------------
    # Proof follow-up (data/submissions/manual_proof.py)
    # ------------------------------------------------------------------

    async def _prompt_for_proof(self, ctx: SlashContext, player, summary: str):
        """Ask for a screenshot iff this proofless submission landed in an
        event's review queue.

        "Qualifies" is decided by reading back the pending ``EventCompletion``
        rows the events worker wrote for this player — the engine's own verdict,
        not a second copy of its matching rules. The worker applies the queue
        asynchronously, so the rows appear a beat after /submit returns; this
        runs after the confirmation embed has already been sent, and only when
        the ``events:active`` gate says an event is running at all.
        """
        conn = getattr(redis_client, "client", None)
        if not events_active(conn):
            return
        rows = []
        try:
            for delay in PROOF_POLL_DELAYS:
                await asyncio.sleep(delay)
                rows = pending_proof_rows(
                    session, [player.player_id], PROOF_PROMPT_LOOKBACK_SECONDS
                )
                if rows:
                    break
        except Exception as e:
            print(f"[submit] proof prompt lookup failed: {e}")
            return
        if not rows:
            return
        stash_awaiting_proof(
            conn, ctx.author.id,
            player_id=player.player_id, player_name=player.player_name,
            completion_ids=[r["id"] for r in rows],
            channel_id=getattr(ctx, "channel_id", None), summary=summary,
        )
        embed = Embed(
            title="📸 One screenshot away",
            description=proof_prompt_text(summary, rows, await self._proof_command_hint()),
            color=_COLOR_PROOF_PROMPT,
        )
        try:
            await ctx.send(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"[submit] proof prompt send failed: {e}")

    async def _proof_command_hint(self) -> str:
        """A clickable `</submit proof:id>` mention when the command id is
        known, else the plain command text."""
        try:
            cmd_id = await get_command_id(self.bot, "submit")
            if str(cmd_id).isdigit():
                return f"</submit proof:{cmd_id}>"
        except Exception:
            pass
        return "`/submit proof`"

    async def _host_proof(self, player, proof: Attachment, entry_id: int):
        """Store a follow-up screenshot where the submission pipeline stores the
        ones attached up front (static/assets/img/user-upload, served at /img)
        and return its public URL. Discord's own CDN links are signed and expire,
        so they can't be persisted as ``EventCompletion.proof_url``."""
        try:
            _path, url = await download_player_image(
                submission_type="proof",
                file_name="proof",
                player=player,
                attachment_url=proof.url,
                file_extension=proof_extension(proof.content_type, proof.filename),
                entry_id=entry_id,
                entry_name="proof",
            )
            return url
        except Exception as e:
            print(f"[submit] proof upload failed: {e}")
            return None

    async def _attach_proof(self, discord_id, proof: Attachment, players, registry):
        """Attach ``proof`` to the caller's still-pending completions and return
        the reply embed. Shared by `/submit proof` and the DM upload path."""
        err = proof_attachment_error(proof.content_type, proof.size)
        if err:
            return self._error_embed(err, title="Couldn't use that screenshot")

        player_ids = [p.player_id for p in players]
        try:
            rows = pending_proof_rows(session, player_ids, PROOF_ATTACH_LOOKBACK_SECONDS)
        except Exception as e:
            print(f"[submit] pending-proof lookup failed: {e}")
            rows = []
        # The registry pins the submission they were prompted about; without it
        # (expired, or they never saw the prompt) fall back to their newest
        # batch of rows still waiting on a screenshot.
        wanted = {int(cid) for cid in ((registry or {}).get("completion_ids") or [])}
        targets = [r for r in rows if r["id"] in wanted] or latest_batch(rows)
        if not targets:
            return self._error_embed(
                "Nothing of yours is waiting on a screenshot right now. Attach one "
                "with the `proof` option when you submit — or if an admin already "
                "reviewed it, ask them to take another look.",
                title="Nothing to attach it to",
            )

        by_id = {p.player_id: p for p in players}
        owner = by_id.get(targets[0]["player_id"]) or players[0]
        url = await self._host_proof(owner, proof, targets[0]["id"])
        if url is None:
            return self._error_embed(
                "Couldn't save that screenshot — try again in a moment.",
                title="Couldn't attach it",
            )
        try:
            updated = attach_proof_url(session, [t["id"] for t in targets], player_ids, url)
        except Exception as e:
            print(f"[submit] proof attach failed: {e}")
            try:
                session.rollback()
            except Exception:
                pass
            return self._error_embed(
                "Couldn't save that screenshot — try again in a moment.",
                title="Couldn't attach it",
            )

        clear_awaiting_proof(getattr(redis_client, "client", None), discord_id)
        if updated <= 0:
            return self._error_embed(
                attached_summary_text(targets, updated), title="Nothing to attach it to"
            )
        embed = Embed(
            title="Screenshot attached",
            description=attached_summary_text(targets, updated),
            color=_COLOR_SUCCESS,
        )
        embed.set_thumbnail(url=url)
        return embed

    # ------------------------------------------------------------------
    # /submit drop
    # ------------------------------------------------------------------

    @slash_command(
        name="submit",
        description="Manually submit something you received to the DropTracker",
        sub_cmd_name="drop",
        sub_cmd_description="Submit a drop you received from an NPC or boss — screenshot proof recommended",
    )
    @slash_option(
        name="item",
        description="The item you received (start typing to search)",
        opt_type=OptionType.STRING,
        required=True,
        autocomplete=True,
        max_length=100,
    )
    @slash_option(
        name="npc",
        description="The NPC or boss it came from (start typing to search)",
        opt_type=OptionType.STRING,
        required=True,
        autocomplete=True,
        max_length=100,
    )
    @_proof_option()
    @_account_option()
    @slash_option(
        name="quantity",
        description="How many you received (default 1)",
        opt_type=OptionType.INTEGER,
        required=False,
        min_value=1,
        max_value=1_000_000,
    )
    @slash_option(
        name="value",
        description="GP value of ONE item — leave blank to use the GE price",
        opt_type=OptionType.INTEGER,
        required=False,
        min_value=0,
    )
    @slash_option(
        name="split_with",
        description="Others you split it with, comma-separated (RSNs)",
        opt_type=OptionType.STRING,
        required=False,
        max_length=300,
    )
    @slash_option(
        name="split_size",
        description="How many people it was split between INCLUDING you (for untracked members)",
        opt_type=OptionType.INTEGER,
        required=False,
        min_value=2,
        max_value=MAX_SPLIT_SIZE,
    )
    async def submit_drop(
        self,
        ctx: SlashContext,
        item: str,
        npc: str,
        account: str | None = None,
        quantity: int | None = None,
        value: int | None = None,
        split_with: str | None = None,
        split_size: int | None = None,
        proof: Attachment | None = None,
    ):
        # split_with and split_size are independent on purpose: naming everyone
        # covers the all-tracked case, while split_size is what lets a share
        # taken by someone not on the DropTracker still count against the
        # divisor (e.g. "4 ways, but only 3 of us are tracked").
        await self._handle_submission(
            ctx, "drop", account, proof,
            item_name=item.strip(), npc_name=npc.strip(),
            quantity=quantity, value=value,
            split_with=split_with, split_size=split_size,
        )

    # ------------------------------------------------------------------
    # /submit clog
    # ------------------------------------------------------------------

    @slash_command(
        name="submit",
        description="Manually submit something you received to the DropTracker",
        sub_cmd_name="clog",
        sub_cmd_description="Submit a new collection log unlock — screenshot proof recommended",
    )
    @slash_option(
        name="item",
        description="The collection log item you unlocked (start typing to search)",
        opt_type=OptionType.STRING,
        required=True,
        autocomplete=True,
        max_length=100,
    )
    @_proof_option()
    @_account_option()
    @slash_option(
        name="source",
        description="The NPC or boss it came from (optional)",
        opt_type=OptionType.STRING,
        required=False,
        autocomplete=True,
        max_length=100,
    )
    @slash_option(
        name="kc",
        description="Your kill count when it dropped (optional)",
        opt_type=OptionType.INTEGER,
        required=False,
        min_value=0,
        max_value=1_000_000,
    )
    async def submit_clog(
        self,
        ctx: SlashContext,
        item: str,
        account: str | None = None,
        source: str | None = None,
        kc: int | None = None,
        proof: Attachment | None = None,
    ):
        await self._handle_submission(
            ctx, "clog", account, proof,
            item_name=item.strip(), npc_name=(source or "").strip() or None, kc=kc,
        )

    # ------------------------------------------------------------------
    # /submit pb
    # ------------------------------------------------------------------

    @slash_command(
        name="submit",
        description="Manually submit something you received to the DropTracker",
        sub_cmd_name="pb",
        sub_cmd_description="Submit a new personal best kill time — screenshot proof recommended",
    )
    @slash_option(
        name="boss",
        description="The boss the personal best is for (start typing to search)",
        opt_type=OptionType.STRING,
        required=True,
        autocomplete=True,
        max_length=100,
    )
    @slash_option(
        name="time",
        description="The kill time, e.g. 1:23.40 (minutes:seconds, decimals allowed)",
        opt_type=OptionType.STRING,
        required=True,
        max_length=20,
    )
    @_proof_option()
    @_account_option()
    @slash_option(
        name="team_size",
        description="Team size (leave blank for solo)",
        opt_type=OptionType.INTEGER,
        required=False,
        min_value=1,
        max_value=100,
    )
    async def submit_pb(
        self,
        ctx: SlashContext,
        boss: str,
        time: str,
        account: str | None = None,
        team_size: int | None = None,
        proof: Attachment | None = None,
    ):
        time_ms = parse_kill_time_ms(time)
        if time_ms is None:
            return await ctx.send(
                embed=self._error_embed(
                    f"`{time}` doesn't look like a kill time — try a format like "
                    "`1:23.40` (minutes:seconds) or `45.6` (seconds)."
                ),
                ephemeral=True,
            )
        await self._handle_submission(
            ctx, "pb", account, proof,
            npc_name=boss.strip(), time_ms=time_ms, team_size=team_size,
        )

    # ------------------------------------------------------------------
    # /submit ca
    # ------------------------------------------------------------------

    @slash_command(
        name="submit",
        description="Manually submit something you received to the DropTracker",
        sub_cmd_name="ca",
        sub_cmd_description="Submit a completed combat achievement task — screenshot proof recommended",
    )
    @slash_option(
        name="task",
        description="The combat achievement task name, e.g. Perfect Zulrah",
        opt_type=OptionType.STRING,
        required=True,
        min_length=3,
        max_length=120,
    )
    @slash_option(
        name="tier",
        description="The task's tier",
        opt_type=OptionType.STRING,
        required=True,
        choices=[SlashCommandChoice(name=t.capitalize(), value=t) for t in CA_TIERS],
    )
    @_proof_option()
    @_account_option()
    async def submit_ca(
        self,
        ctx: SlashContext,
        task: str,
        tier: str,
        account: str | None = None,
        proof: Attachment | None = None,
    ):
        await self._handle_submission(
            ctx, "ca", account, proof, task=task, tier=tier,
        )

    # ------------------------------------------------------------------
    # /submit pet
    # ------------------------------------------------------------------

    @slash_command(
        name="submit",
        description="Manually submit something you received to the DropTracker",
        sub_cmd_name="pet",
        sub_cmd_description="Submit a pet drop — screenshot proof recommended",
    )
    @slash_option(
        name="pet",
        description="The pet you received (start typing to search, e.g. Vorki)",
        opt_type=OptionType.STRING,
        required=True,
        autocomplete=True,
        max_length=100,
    )
    @_proof_option()
    @_account_option()
    @slash_option(
        name="source",
        description="The NPC or boss it came from (optional)",
        opt_type=OptionType.STRING,
        required=False,
        autocomplete=True,
        max_length=100,
    )
    @slash_option(
        name="kc",
        description="Your kill count when it dropped (optional)",
        opt_type=OptionType.INTEGER,
        required=False,
        min_value=0,
        max_value=1_000_000,
    )
    async def submit_pet(
        self,
        ctx: SlashContext,
        pet: str,
        account: str | None = None,
        source: str | None = None,
        kc: int | None = None,
        proof: Attachment | None = None,
    ):
        await self._handle_submission(
            ctx, "pet", account, proof,
            item_name=pet.strip(), npc_name=(source or "").strip() or None, kc=kc,
        )

    # ------------------------------------------------------------------
    # /submit proof
    # ------------------------------------------------------------------

    @slash_command(
        name="submit",
        description="Manually submit something you received to the DropTracker",
        sub_cmd_name="proof",
        sub_cmd_description="Add a screenshot to a submission of yours that is waiting for an admin to review it",
    )
    @slash_option(
        name="screenshot",
        description="The screenshot proving the submission you already made (PNG/JPEG/WebP/GIF, max 10 MB)",
        opt_type=OptionType.ATTACHMENT,
        required=True,
    )
    async def submit_proof(self, ctx: SlashContext, screenshot: Attachment):
        """Attach proof after the fact, so a held submission doesn't have to be
        made again. A modal can't take a file and neither can a button, so a
        command option is the deterministic in-channel way to hand us an upload —
        the bot does receive guild messages now (GUILD_MESSAGES added 2026-08-20
        for the clan chat bridge), but a bare channel upload carries no link to
        a specific held submission, so the explicit command stays."""
        await ctx.defer(ephemeral=True)
        self._refresh_session()

        players, err = self._claimed_players(ctx.author.id)
        if err:
            return await ctx.send(embed=self._error_embed(err), ephemeral=True)

        registry = load_awaiting_proof(getattr(redis_client, "client", None), ctx.author.id)
        embed = await self._attach_proof(ctx.author.id, screenshot, players, registry)
        return await ctx.send(embed=embed, ephemeral=True)

    @listen(MessageCreate)
    async def on_proof_dm(self, event: MessageCreate):
        """Attach a screenshot DM'd to the bot to whatever the sender was just
        prompted about.

        DMs are the deliberate surface for this: guild messages do reach the
        gateway now (GUILD_MESSAGES + MESSAGE_CONTENT since 2026-08-20, for the
        clan chat bridge), but a guild upload can't be tied to a specific held
        submission, so `/submit proof` covers the in-channel case. Gated on the
        awaiting-proof registry, so an unrelated DM costs one Redis GET and
        nothing else.
        """
        message = getattr(event, "message", None)
        if message is None or getattr(message, "_guild_id", None) is not None:
            return
        try:
            author = getattr(message, "author", None)
            if author is None or getattr(author, "bot", False):
                return
            attachments = getattr(message, "attachments", None) or []
            if not attachments:
                return
            conn = getattr(redis_client, "client", None)
            registry = load_awaiting_proof(conn, author.id)
            if registry is None:
                return
            self._refresh_session()
            players, err = self._claimed_players(author.id)
            if err:
                return
            embed = await self._attach_proof(author.id, attachments[0], players, registry)
            await message.reply(embed=embed)
        except Exception as e:
            print(f"[submit] proof DM handling failed: {e}")

    # ------------------------------------------------------------------
    # Autocompletes
    # ------------------------------------------------------------------

    def _search_names(self, sql, pattern: str, dedupe_npc: bool = False) -> list[str]:
        rows = session.execute(sql, {"pat": pattern}).fetchall()
        names: list[str] = []
        seen: set = set()
        for name, _tracked in rows:
            key = npc_match_key(name) if dedupe_npc else name
            if not name or key in seen:
                continue
            seen.add(key)
            names.append(name)
            if len(names) >= 25:
                break
        return names

    async def _send_name_choices(self, ctx: AutocompleteContext, sql, dedupe_npc: bool = False):
        self._refresh_session()
        query = (ctx.input_text or "").strip()
        if len(query) < 2:
            # An unanchored LIKE over the whole catalog is slow (~900 ms) and
            # returns alphabetical noise; wait until the search is meaningful.
            return await ctx.send(choices=[])
        try:
            names = self._search_names(sql, f"%{query}%", dedupe_npc=dedupe_npc)
        except Exception:
            names = []
        await ctx.send(choices=[{"name": n[:100], "value": n[:100]} for n in names])

    @submit_drop.autocomplete("item")
    @submit_clog.autocomplete("item")
    async def item_autocomplete(self, ctx: AutocompleteContext):
        await self._send_name_choices(ctx, _ITEM_AC_SQL)

    @submit_pet.autocomplete("pet")
    async def pet_autocomplete(self, ctx: AutocompleteContext):
        await self._send_name_choices(ctx, _ITEM_AC_SQL)

    @submit_drop.autocomplete("npc")
    @submit_pb.autocomplete("boss")
    @submit_clog.autocomplete("source")
    @submit_pet.autocomplete("source")
    async def npc_autocomplete(self, ctx: AutocompleteContext):
        await self._send_name_choices(ctx, _NPC_AC_SQL, dedupe_npc=True)

    @submit_drop.autocomplete("account")
    @submit_clog.autocomplete("account")
    @submit_pb.autocomplete("account")
    @submit_ca.autocomplete("account")
    @submit_pet.autocomplete("account")
    async def account_autocomplete(self, ctx: AutocompleteContext):
        self._refresh_session()
        try:
            user = session.query(User).filter(User.discord_id == str(ctx.author.id)).first()
            if user is None:
                return await ctx.send(choices=[])
            players = (
                session.query(Player)
                .filter(Player.user_id == user.user_id)
                .order_by(Player.player_name)
                .all()
            )
            query = (ctx.input_text or "").strip().lower()
            names = [
                p.player_name for p in players
                if p.player_name and (not query or query in p.player_name.lower())
            ]
        except Exception:
            names = []
        await ctx.send(choices=[{"name": n, "value": n} for n in names[:25]])
