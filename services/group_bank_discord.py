"""Clan bank on Discord (suggestion #230): activity posts and a standing board.

Two surfaces, both opt-in by picking a channel in the group's bank settings:

* **Activity posts** (``bank_post_channel_id``). A message each time GP is
  recorded in the bank: a donation by default, and withdrawals, payouts and
  event funding too with ``bank_post_outflows``. Queued into ``discord_outbox``
  in the same transaction that confirms the entry (the Web API never talks to
  Discord, and a rolled-back write never posts), then sent by the core bot.
* **The bank board** (``bank_board_channel_id``). One bot-owned message edited
  in place with the balance, totals, top donors and recent activity: the clan
  bank card from the group page. Every bank write marks the group dirty in
  Redis; the core bot's ``bank_board_updates`` task drains that set each minute
  and sweeps every board now and then, editing only when the content changed
  (the same shape as ``services/clan_log_discord``).

An entry posts once, the first time it is confirmed. Entries dated more than
:data:`HISTORY_AFTER` before their confirmation are history a clan is typing
in, not news, so they update the board without a post. Synced coffer
withdrawals don't post while ``bank_coffer_withdrawals_neutral`` is on, the
same rule that keeps them out of the totals. Names follow ``bank_show_donors``:
with it off, posts and the board show amounts only.

Stdlib-only at import time (DB, Redis and Discord are imported lazily), so the
unit-test conftest can load the real module.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Iterable, Optional

POST_CHANNEL_KEY = "bank_post_channel_id"
POST_OUTFLOWS_KEY = "bank_post_outflows"
BOARD_CHANNEL_KEY = "bank_board_channel_id"
# "<channel_id>:<message_id>" of the board message. Written by the bot only.
BOARD_MESSAGE_KEY = "bank_board_message_id"

DIRTY_KEY = "bank:board:dirty"
_POSTED_HASH_KEY = "bank:board:{group_id}:posted_hash"
_POSTED_HASH_TTL = 30 * 24 * 3600

#: An entry dated this long before it was confirmed is history, not news.
HISTORY_AFTER = timedelta(days=7)

#: Boards edited per bot tick; the rest wait for the next one.
MAX_BOARDS_PER_CYCLE = 8
#: Dirty groups taken off the set per tick.
MAX_DIRTY_PER_CYCLE = 50

BOARD_RECENT_LIMIT = 8
BOARD_DONORS_LIMIT = 10

_COLOR_IN = 0x2ECC71
_COLOR_OUT = 0xE67E22
_COLOR_BOARD = 0xD4AF37

_OUTFLOW_KINDS = ("withdrawal", "payout", "event_transfer")
_FIELD_LIMIT = 1024
_UNNAMED = "A clan member"


def _unset(value) -> bool:
    return value is None or str(value).strip() in ("", "0", "None")


def channel_id_from(value) -> Optional[str]:
    """A configured channel as a bare id, or None when unset or not a channel."""
    if _unset(value):
        return None
    try:
        from web_api.config_registry import parse_channel_ref

        return parse_channel_ref(value) or None
    except Exception:
        raw = str(value).strip()
        return raw if raw.isdigit() else None


def gp(amount) -> str:
    """``5,000,000 gp``, always positive (the wording carries the direction)."""
    return f"{abs(int(amount or 0)):,} gp"


def short_gp(amount) -> str:
    """``5M`` / ``1.25B`` / ``750K`` for dense lists."""
    n = abs(int(amount or 0))
    for size, suffix in ((10 ** 9, "B"), (10 ** 6, "M"), (10 ** 3, "K")):
        if n >= size:
            text = f"{n / size:.2f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return f"{n:,}"


def is_postable(kind: str, source: Optional[str], created_at: Optional[datetime],
                confirmed_at: datetime, *, outflows: bool, neutral: bool) -> bool:
    """Whether a freshly confirmed entry gets an activity post."""
    from services.group_bank import is_coffer_move

    if kind == "donation":
        pass
    elif kind in _OUTFLOW_KINDS:
        if not outflows or is_coffer_move(kind, source, neutral):
            return False
    else:
        # Adjustments are bookkeeping corrections, not something that happened.
        return False
    if created_at is not None and confirmed_at - created_at > HISTORY_AFTER:
        return False
    return True


def md_escape(text: str) -> str:
    """A player name safe inside Discord Markdown (``_`` and ``*`` are common
    in names and would otherwise turn into italics)."""
    out = str(text)
    for ch in ("\\", "*", "_", "~", "`", "|", ">"):
        out = out.replace(ch, "\\" + ch)
    return out


def board_title(group_name: str) -> str:
    """``Venture Clan bank``, not ``Venture Clan clan bank``."""
    name = (group_name or "").strip() or "Your clan"
    return f"{name} bank" if name.lower().endswith("clan") else f"{name} clan bank"


def activity_line(kind: str, source: Optional[str], amount, name: Optional[str]) -> str:
    """One sentence for an entry, e.g. ``**Zezima** donated **5,000,000 gp**``."""
    who = f"**{md_escape(name)}**" if name else _UNNAMED
    value = f"**{gp(amount)}**"
    coffer = source == "coffer"
    if kind == "donation":
        return f"{who} deposited {value} into the coffer" if coffer else f"{who} donated {value}"
    if kind == "withdrawal":
        if coffer:
            return f"{who} withdrew {value} from the coffer"
        return f"{value} withdrawn" + (f" by {who}" if name else "")
    if kind == "payout":
        return f"{who} was paid {value}" if name else f"{value} paid out"
    if kind == "event_transfer":
        return f"{value} moved into an event prize pot"
    verb = "added to" if int(amount or 0) >= 0 else "taken from"
    return f"{value} {verb} the bank"


def entry_embed(*, group_id: int, group_name: str, kind: str, source: Optional[str],
                amount, name: Optional[str], balance: int, proof_url: Optional[str],
                when: Optional[datetime]) -> dict:
    """The activity post for one entry, as an embed dict for the outbox."""
    from utils.site_urls import group_url

    inflow = kind == "donation" or (kind == "adjustment" and int(amount or 0) >= 0)
    embed = {
        "title": "Clan bank donation" if kind == "donation" else "Clan bank",
        "url": f"{group_url(group_id)}#bank",
        "description": activity_line(kind, source, amount, name) + ".",
        "color": _COLOR_IN if inflow else _COLOR_OUT,
        "fields": [{"name": "Bank balance", "value": gp_signed(balance), "inline": True}],
        "footer": {"text": f"{group_name} · DropTracker.io"},
    }
    if when is not None:
        embed["timestamp"] = when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if proof_url and str(proof_url).startswith("https://"):
        embed["image"] = {"url": proof_url}
    return embed


def gp_signed(amount) -> str:
    n = int(amount or 0)
    return f"-{gp(n)}" if n < 0 else gp(n)


def _clip_lines(lines: Iterable[str], limit: int = _FIELD_LIMIT) -> str:
    out, size = [], 0
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def board_embed(*, group_id: int, group_name: str, summary: dict,
                recent: list, show_names: bool) -> dict:
    """The standing board. ``summary`` is the route's ``_summary`` output and
    ``recent`` is ``[{kind, source, amount, rsn, created_at}]`` newest first.
    Carries no timestamp, so the same bank always builds the same embed."""
    from utils.site_urls import group_url

    totals = summary.get("totals") or {}
    fields = [
        {"name": "Donated", "value": gp(totals.get("donated")), "inline": True},
        {"name": "Taken out", "value": gp(totals.get("paid_out")), "inline": True},
        {"name": "Donors", "value": f"{int(summary.get('donor_count') or 0):,}", "inline": True},
    ]
    held = int(summary.get("held_outside_coffer") or 0)
    if summary.get("coffer_withdrawals_neutral") and held:
        fields.append({"name": "Held outside the coffer", "value": gp(held), "inline": True})
    if show_names:
        donors = summary.get("top_donors") or []
        if donors:
            fields.append({
                "name": "Top donors",
                "value": _clip_lines(
                    f"{i}. {md_escape(d.get('rsn') or 'Unknown')}: {short_gp(d.get('total'))}"
                    for i, d in enumerate(donors[:BOARD_DONORS_LIMIT], start=1)
                ),
                "inline": False,
            })
    if recent:
        lines = []
        for r in recent[:BOARD_RECENT_LIMIT]:
            stamp = r.get("created_at")
            when = f"<t:{int(stamp)}:R> " if stamp else ""
            name = r.get("rsn") if show_names else None
            lines.append(when + activity_line(r.get("kind"), r.get("source"), r.get("amount"), name))
        fields.append({"name": "Recent activity", "value": _clip_lines(lines), "inline": False})
    has_activity = bool(totals.get("by_kind"))
    return {
        "title": board_title(group_name),
        "url": f"{group_url(group_id)}#bank",
        "description": (
            f"Balance: **{gp_signed(totals.get('balance'))}**"
            if has_activity else "Nothing has been recorded in the bank yet."
        ),
        "color": _COLOR_BOARD,
        "fields": fields,
        "footer": {"text": "Updates automatically · DropTracker.io"},
    }


def board_hash(channel_id: str, embed: dict) -> str:
    """Change detector for the board. Covers the channel, so moving the board
    to another channel reads as a change and posts it there."""
    raw = json.dumps({"c": str(channel_id), "e": embed}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()


def parse_message_ref(raw) -> tuple:
    """``(channel_id, message_id)`` from the stored board ref, or ``(None, None)``."""
    if _unset(raw):
        return None, None
    channel_id, sep, message_id = str(raw).strip().partition(":")
    if not sep or not channel_id.isdigit() or not message_id.isdigit():
        return None, None
    return channel_id, message_id


# --------------------------------------------------------------------------- #
# Write side: called by the bank's writers (web_api routes, coffer sync)
# --------------------------------------------------------------------------- #
def mark_board_dirty(group_id: int) -> None:
    """Ask the core bot to look at this group's board soon. Best-effort: the
    periodic sweep catches anything a lost mark would have."""
    try:
        from utils.redis import redis_client

        redis_client.client.sadd(DIRTY_KEY, str(int(group_id)))
    except Exception:
        pass


def _balance(session, group_id: int, neutral: bool) -> int:
    """Confirmed balance, the same number as the route's headline: every
    confirmed row added up, less synced coffer withdrawals while they're
    neutral. Runs inside the writer's transaction, so it includes the row
    being confirmed (autoflush)."""
    from sqlalchemy import and_, func

    from db import GroupBankEntry

    q = session.query(func.coalesce(func.sum(GroupBankEntry.amount), 0)).filter(
        GroupBankEntry.group_id == group_id, GroupBankEntry.status == "confirmed",
    )
    if neutral:
        q = q.filter(~and_(GroupBankEntry.kind == "withdrawal", GroupBankEntry.source == "coffer"))
    return int(q.scalar() or 0)


def queue_entry_posts(session, group_id: int, entries: list) -> int:
    """Queue activity posts for entries confirmed for the first time in the
    caller's transaction. Adds outbox rows without committing, so they commit
    (or roll back) with the entries. Never raises: a post is not worth failing
    a bank write over. Returns the number queued."""
    if not entries:
        return 0
    try:
        from utils import group_config as gc

        config = gc.get_bulk(
            session, [group_id],
            [POST_CHANNEL_KEY, POST_OUTFLOWS_KEY, "bank_show_donors",
             "bank_coffer_withdrawals_neutral"],
        )
        channel_id = channel_id_from(config.get((group_id, POST_CHANNEL_KEY)))
        if not channel_id:
            return 0
        outflows = gc.is_truthy(config.get((group_id, POST_OUTFLOWS_KEY)))
        neutral = gc.is_truthy(config.get((group_id, "bank_coffer_withdrawals_neutral")))
        # Unset means on, as on the group page (route _bank_visibility).
        raw_names = config.get((group_id, "bank_show_donors"))
        show_names = raw_names is None or str(raw_names).strip() == "" or gc.is_truthy(raw_names)

        now = datetime.utcnow()
        wanted = [
            e for e in entries
            if is_postable(e.kind, e.source, e.created_at, e.confirmed_at or now,
                           outflows=outflows, neutral=neutral)
        ]
        if not wanted:
            return 0

        from db import Group, Player
        from services.discord_outbox import enqueue

        group_name = (
            session.query(Group.group_name).filter(Group.group_id == group_id).scalar()
            or "Your clan"
        )
        pids = {e.player_id for e in wanted if e.player_id is not None}
        names = dict(
            session.query(Player.player_id, Player.player_name)
            .filter(Player.player_id.in_(pids)).all()
        ) if pids else {}
        balance = _balance(session, group_id, neutral)
        for e in wanted:
            name = (names.get(e.player_id) or e.rsn) if show_names else None
            enqueue(
                session,
                channel_id=channel_id,
                embed=entry_embed(
                    group_id=group_id, group_name=group_name, kind=e.kind,
                    source=e.source, amount=e.amount, name=name, balance=balance,
                    proof_url=e.proof_url, when=e.created_at,
                ),
                ref_type="group_bank_entry",
                ref_id=e.id,
                commit=False,
            )
        return len(wanted)
    except Exception as exc:
        try:
            from db.app_logger import AppLogger

            AppLogger().log(
                log_type="error",
                data=f"Clan bank post not queued for group {group_id}: {exc}",
                app_name="group_bank_discord", description="queue_entry_posts")
        except Exception:
            pass
        return 0


# --------------------------------------------------------------------------- #
# Board: run by the core bot
# --------------------------------------------------------------------------- #
def _board_groups(session, group_ids: Optional[list] = None) -> dict:
    """``{group_id: channel_id}`` for groups with a board channel set,
    limited to ``group_ids`` when given."""
    from db.models import GroupConfiguration

    q = session.query(GroupConfiguration.group_id, GroupConfiguration.config_value).filter(
        GroupConfiguration.config_key == BOARD_CHANNEL_KEY,
    )
    if group_ids is not None:
        if not group_ids:
            return {}
        q = q.filter(GroupConfiguration.group_id.in_(group_ids))
    out = {}
    for gid, value in q.all():
        channel_id = channel_id_from(value)
        if channel_id:
            out[int(gid)] = channel_id
    return out


def build_board(session, group_id: int, channel_id: str) -> Optional[dict]:
    """``{"embed", "hash"}`` for a group's board, from the same queries as
    the group page's bank card."""
    from db import Group
    from web_api.routes.group_bank import _bank_visibility, _decorate, _not_coffer_move, _summary
    from db import GroupBankEntry

    group_name = session.query(Group.group_name).filter(Group.group_id == group_id).scalar()
    if group_name is None:
        return None
    _show_on_profile, show_names = _bank_visibility(session, group_id)
    summary = _summary(session, group_id)
    q = session.query(GroupBankEntry).filter(
        GroupBankEntry.group_id == group_id, GroupBankEntry.status == "confirmed",
    )
    if summary.get("coffer_withdrawals_neutral"):
        q = q.filter(_not_coffer_move())
    rows = (
        q.order_by(GroupBankEntry.created_at.desc(), GroupBankEntry.id.desc())
        .limit(BOARD_RECENT_LIMIT)
        .all()
    )
    recent = [
        {"kind": r["kind"], "source": r["source"], "amount": _money_value(r["amount"]),
         "rsn": r["rsn"], "created_at": r["created_at"]}
        for r in _decorate(session, rows, False)
    ]
    embed = board_embed(group_id=group_id, group_name=group_name, summary=summary,
                        recent=recent, show_names=show_names)
    return {"embed": embed, "hash": board_hash(channel_id, embed)}


def _money_value(value) -> int:
    """``web_api.common.money`` output (``{value, value_formatted}``) back to an int."""
    if isinstance(value, dict):
        return int(value.get("value") or 0)
    return int(value or 0)


def _as_group_id(raw) -> Optional[int]:
    """A dirty-set member (bytes from the raw client) as a group id."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _scan(full: bool) -> list:
    """Boards that changed: ``[(group_id, channel_id, embed, hash, stored_ref)]``.
    Blocking DB work; MUST run off the event loop. Owns its session."""
    from db.models.base import Session
    from utils import group_config as gc
    from utils.redis import redis_client

    group_ids = None
    if not full:
        try:
            popped = redis_client.client.spop(DIRTY_KEY, MAX_DIRTY_PER_CYCLE) or []
        except Exception:
            popped = []
        group_ids = sorted({gid for gid in map(_as_group_id, popped) if gid is not None})
        if not group_ids:
            return []

    session = Session()
    try:
        channels = _board_groups(session, group_ids)
        if not channels:
            return []
        refs = gc.get_bulk(session, list(channels), [BOARD_MESSAGE_KEY])
        out = []
        for group_id, channel_id in channels.items():
            try:
                board = build_board(session, group_id, channel_id)
            except Exception as exc:
                session.rollback()
                print(f"[bank_board] build failed for group {group_id}: {exc}")
                continue
            if board is None:
                continue
            try:
                posted = redis_client.get(_POSTED_HASH_KEY.format(group_id=group_id))
            except Exception:
                posted = None
            stored = refs.get((group_id, BOARD_MESSAGE_KEY))
            if posted == board["hash"] and parse_message_ref(stored)[0] == channel_id:
                continue
            out.append((group_id, channel_id, board["embed"], board["hash"], stored))
        # A group with no board on Discord yet goes first.
        out.sort(key=lambda c: 1 if parse_message_ref(c[4])[0] else 0)
        return out
    finally:
        session.close()


def _save_message_ref(group_id: int, ref: Optional[str]) -> None:
    from db.models import GroupConfiguration
    from db.models.base import Session
    from utils import group_config

    session = Session()
    try:
        row = (
            session.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == BOARD_MESSAGE_KEY)
            .first()
        )
        if row is None:
            if ref is None:
                return
            session.add(GroupConfiguration(group_id=group_id, config_key=BOARD_MESSAGE_KEY,
                                           config_value=ref))
        else:
            row.config_value = ref or ""
        session.commit()
    finally:
        session.close()
    group_config.invalidate(group_id, BOARD_MESSAGE_KEY)


async def refresh_boards(bot, *, full: bool = False, limit: int = MAX_BOARDS_PER_CYCLE) -> dict:
    """Post or edit the bank board for every group whose board changed.

    ``full`` sweeps every group with a board channel (picks up a newly set
    channel and anything a lost dirty mark missed); otherwise only the groups
    marked dirty since the last tick. Groups past ``limit`` are marked dirty
    again for the next tick."""
    from interactions import Embed

    from utils.discord_threads import PROBLEM_STATES, ensure_thread_open
    from utils.redis import redis_client

    stats = {"candidates": 0, "posted": 0, "edited": 0, "failed": 0, "skipped": 0}
    candidates = await asyncio.to_thread(_scan, full)
    stats["candidates"] = len(candidates)
    for group_id, *_rest in candidates[limit:]:
        mark_board_dirty(group_id)

    for group_id, channel_id, embed_dict, state_hash, stored in candidates[:limit]:
        try:
            channel = await bot.fetch_channel(channel_id=channel_id)
            if not channel:
                stats["skipped"] += 1
                continue
            thread_state = await ensure_thread_open(
                bot.http, channel, group_id=group_id, feature="bank_board",
            )
            if thread_state in PROBLEM_STATES:
                stats["skipped"] += 1
                continue
            embed = Embed.from_dict(embed_dict)
            old_channel_id, old_message_id = parse_message_ref(stored)
            message = None
            if old_message_id and old_channel_id == channel_id:
                try:
                    message = await channel.fetch_message(message_id=old_message_id)
                except Exception:
                    message = None  # deleted: post a fresh one
                if message is not None and int(message.author.id) != int(bot.user.id):
                    message = None
            if message is not None:
                await message.edit(embeds=[embed])
                stats["edited"] += 1
            else:
                message = await channel.send(embeds=[embed])
                await asyncio.to_thread(_save_message_ref, group_id, f"{channel_id}:{message.id}")
                stats["posted"] += 1
                if old_message_id and old_channel_id != channel_id:
                    # The board moved channels: tidy up the old one.
                    try:
                        old = await bot.fetch_channel(channel_id=old_channel_id)
                        old_msg = await old.fetch_message(message_id=old_message_id) if old else None
                        if old_msg is not None and int(old_msg.author.id) == int(bot.user.id):
                            await old_msg.delete()
                    except Exception:
                        pass
            redis_client.client.setex(
                _POSTED_HASH_KEY.format(group_id=group_id), _POSTED_HASH_TTL, state_hash,
            )
        except Exception as exc:
            stats["failed"] += 1
            print(f"[bank_board] group {group_id} failed: {exc}")
    return stats
