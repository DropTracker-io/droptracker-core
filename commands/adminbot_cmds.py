"""Owner-only Admin / Knowledgebase slash commands (KBAdminCommands extension).

Loaded ONLY by ``bots/adminbot.py`` (``bot.load_extension("commands.adminbot_cmds")``)
— never registered on the public bot. Every command is guild-scoped (instant
registration), owner-gated, and replies ephemerally.

The KB service modules (``services.kb.retriever`` / ``answerer`` / ``miner``) are
built in parallel and may pull in optional dependencies, so they are imported
*inside* each handler (deferred) and never at module import time. Only stdlib,
``interactions``, ``sqlalchemy`` and the ORM models are imported at module top.
"""

import os
import re
import json
import asyncio

from interactions import (
    Extension,
    SlashContext,
    Embed,
    OptionType,
    slash_command,
    slash_option,
    SlashCommandChoice,
)
from sqlalchemy import func, or_, desc

from db.models import (
    Session,
    User,
    Player,
    Group,
    GroupConfiguration,
    Drop,
    ItemList,
    NpcList,
    Ticket,
    TicketMessage,
    KBIngestState,
    AuditLog,
    user_group_association,
)
from db.models.base import engine

# Guild-scoped registration = instant (no ~1h global propagation). Defaults to the
# DropTracker HQ guild; overridable via ADMIN_BOT_GUILD_IDS (comma-separated).
SCOPES = [int(x) for x in os.getenv("ADMIN_BOT_GUILD_IDS", "1172737525069135962").split(",") if x.strip()]

# Discord IDs allowed to operate this bot = web superadmins (WEB_SUPERADMIN_
# DISCORD_IDS, shared with the website) UNION bot-only operators (ADMIN_BOT_
# OPERATOR_IDS — grants admin-bot access WITHOUT web-superadmin powers), plus
# (DB fallback) any User row flagged is_superadmin. Keeping a dedicated operator
# list means "can run owner commands" is explicit/greppable, not dependent on a
# mutable DB flag, and least-privilege (no accidental site-superadmin grant).
OWNER_IDS = {
    x.strip()
    for x in (
        os.getenv("WEB_SUPERADMIN_DISCORD_IDS", "").split(",")
        + os.getenv("ADMIN_BOT_OPERATOR_IDS", "").split(",")
    )
    if x.strip()
}

_EMBED_COLOR = 0x5865F2


def _is_owner_discord_id(discord_id: str) -> bool:
    if discord_id in OWNER_IDS:
        return True
    try:
        with Session() as s:
            u = s.query(User).filter(User.discord_id == str(discord_id)).first()
            return bool(u and getattr(u, "is_superadmin", False))
    except Exception:
        return False


def _audit(actor_discord_id: str, action: str, target: str = "", before=None, after=None) -> None:
    """Best-effort audit trail — must NEVER raise into a command."""
    try:
        with Session() as s:
            uid = None
            try:
                row = s.query(User.user_id).filter(User.discord_id == str(actor_discord_id)).first()
                uid = row[0] if row else None
            except Exception:
                pass
            s.add(AuditLog(
                actor_user_id=uid, group_id=None,
                action=action[:64],
                target=(target or "")[:128] or None,
                before=json.dumps(before)[:60000] if before is not None else None,
                after=json.dumps(after)[:60000] if after is not None else None,
            ))
            s.commit()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Read-only SQL surface (/sql). Canonical validation + execution now live in
# services/kb/sql_guard.py (dependency-light, safe at module top) and are shared
# with services.kb.investigator's model-generated investigation queries.
# --------------------------------------------------------------------------- #
from services.kb.sql_guard import validate_readonly_sql, run_readonly_sql  # noqa: E402


# --------------------------------------------------------------------------- #
# Small formatting / access helpers
# --------------------------------------------------------------------------- #
def _g(obj, key, default=None):
    """Read ``key`` from a dict OR an object (retriever/answerer results may be
    either — they are written in parallel)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _dt(x) -> str:
    try:
        return x.strftime("%Y-%m-%d %H:%M") if x else "-"
    except Exception:
        return str(x) if x else "-"


def _split_chunks(text: str, size: int):
    return [text[i:i + size] for i in range(0, len(text), size)]


def _cell(v) -> str:
    if v is None:
        return "NULL"
    try:
        s = str(v)
    except Exception:
        s = repr(v)
    s = s.replace("\n", " ").replace("\r", " ")
    return s[:40]


def _fmt_table(cols, rows) -> str:
    cols = [str(c) for c in cols]
    srows = [[_cell(v) for v in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in srows:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))

    def fmt(vals):
        return " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(vals) if i < len(widths))

    lines = [fmt(cols), "-+-".join("-" * w for w in widths)]
    for r in srows:
        lines.append(fmt(r))
    return "\n".join(lines)


def _render_stats(st) -> str:
    if isinstance(st, dict):
        lines = []
        for k, v in st.items():
            if isinstance(v, dict):
                inner = ", ".join(f"{ik}={iv}" for ik, iv in v.items()) or "—"
                lines.append(f"{k}: {inner}")
            elif isinstance(v, (list, tuple)):
                lines.append(f"{k}: {', '.join(str(x) for x in v)}")
            else:
                lines.append(f"{k}: {v}")
        return "\n".join(lines) if lines else "(no stats)"
    return str(st)


def _ingest_states():
    """KBIngestState rows as (source_ref, status, last_synced_at) tuples."""
    out = []
    try:
        with Session() as s:
            for r in (s.query(KBIngestState)
                        .order_by(KBIngestState.source_ref)
                        .limit(50).all()):
                out.append((r.source_ref, r.status, r.last_synced_at))
    except Exception:
        pass
    return out


def _lookup_sync(kind: str, q: str) -> str:
    """READ-ONLY lookup across the core ORM models. One short-lived Session, every
    query LIMITed. Returns a plain-text block (caller wraps it in a code fence)."""
    q = (q or "").strip()
    if not q:
        return "Empty query."
    lines = []
    try:
        with Session() as s:
            if kind == "player":
                p = None
                if q.isdigit():
                    p = s.query(Player).filter(Player.player_id == int(q)).first()
                if p is None:
                    p = s.query(Player).filter(Player.player_name.ilike(f"%{q}%")).first()
                if not p:
                    return f"No player matching '{q}'."
                lines.append(f"PLAYER #{p.player_id}  {p.player_name}")
                lines.append(f"wom_id: {p.wom_id}   account_hash: {'present' if p.account_hash else 'none'}")
                lines.append(f"total_level: {p.total_level}   ehb: {p.ehb}   hidden: {p.hidden}")
                lines.append(f"added: {_dt(p.date_added)}   updated: {_dt(p.date_updated)}")
                u = None
                if p.user_id:
                    u = s.query(User).filter(User.user_id == p.user_id).first()
                if u:
                    lines.append(f"user: #{u.user_id} {u.username or '-'} (discord {u.discord_id or '-'})")
                else:
                    lines.append("user: (none linked)")
                grp_names = [r[0] for r in (
                    s.query(Group.group_name)
                     .join(user_group_association, user_group_association.c.group_id == Group.group_id)
                     .filter(user_group_association.c.player_id == p.player_id)
                     .limit(15).all())]
                lines.append(f"groups: {', '.join(n for n in grp_names if n) if grp_names else '(none)'}")
                drop_count = s.query(func.count(Drop.drop_id)).filter(Drop.player_id == p.player_id).scalar() or 0
                lines.append(f"drops: {drop_count:,}")
                recent = (
                    s.query(Drop.value, Drop.date_added, ItemList.item_name, NpcList.npc_name)
                     .outerjoin(ItemList, ItemList.item_id == Drop.item_id)
                     .outerjoin(NpcList, NpcList.npc_id == Drop.npc_id)
                     .filter(Drop.player_id == p.player_id)
                     .order_by(desc(Drop.date_added))
                     .limit(5).all())
                if recent:
                    lines.append("recent drops:")
                    for val, dadd, iname, nname in recent:
                        lines.append(f"  - {iname or '?'} from {nname or '?'}  {(val or 0):,}gp  {_dt(dadd)}")
                return "\n".join(lines)

            if kind == "group":
                g = None
                if q.isdigit():
                    g = s.query(Group).filter(Group.group_id == int(q)).first()
                if g is None:
                    g = s.query(Group).filter(Group.group_name.ilike(f"%{q}%")).first()
                if not g:
                    return f"No group matching '{q}'."
                lines.append(f"GROUP #{g.group_id}  {g.group_name}")
                lines.append(f"wom_id: {g.wom_id}   guild_id: {g.guild_id}")
                lines.append(f"added: {_dt(g.date_added)}   updated: {_dt(g.date_updated)}")
                player_count = (
                    s.query(func.count(user_group_association.c.player_id))
                     .filter(user_group_association.c.group_id == g.group_id,
                             user_group_association.c.player_id.isnot(None))
                     .scalar()) or 0
                member_count = (
                    s.query(func.count(func.distinct(user_group_association.c.user_id)))
                     .filter(user_group_association.c.group_id == g.group_id,
                             user_group_association.c.user_id.isnot(None))
                     .scalar()) or 0
                lines.append(f"players: {player_count:,}   linked users: {member_count:,}")
                for key in ("channel_id_to_post_loot", "minimum_value_to_notify", "lootboard_channel_id"):
                    row = (s.query(GroupConfiguration.config_value)
                             .filter(GroupConfiguration.group_id == g.group_id,
                                     GroupConfiguration.config_key == key)
                             .first())
                    lines.append(f"{key}: {row[0] if row else '(unset)'}")
                return "\n".join(lines)

            if kind == "user":
                u = None
                if q.isdigit():
                    u = s.query(User).filter(or_(User.discord_id == q, User.user_id == int(q))).first()
                if u is None:
                    u = s.query(User).filter(User.username.ilike(f"%{q}%")).first()
                if not u:
                    return f"No user matching '{q}'."
                lines.append(f"USER #{u.user_id}  {u.username or '-'}")
                lines.append(f"discord_id: {u.discord_id or '-'}")
                lines.append(f"added: {_dt(u.date_added)}   updated: {_dt(u.date_updated)}")
                lines.append(f"is_superadmin: {getattr(u, 'is_superadmin', 'n/a')}")
                players = [r[0] for r in (
                    s.query(Player.player_name).filter(Player.user_id == u.user_id).limit(25).all())]
                lines.append(f"players: {', '.join(n for n in players if n) if players else '(none)'}")
                return "\n".join(lines)

            if kind == "ticket":
                if not q.isdigit():
                    return "Ticket lookup requires a numeric ticket_id."
                t = s.query(Ticket).filter(Ticket.ticket_id == int(q)).first()
                if not t:
                    return f"No ticket #{q}."

                def uname(uid):
                    if not uid:
                        return "-"
                    r = s.query(User.username, User.discord_id).filter(User.user_id == uid).first()
                    if not r:
                        return f"user#{uid}"
                    return f"{r[0] or '-'} (discord {r[1] or '-'})"

                lines.append(f"TICKET #{t.ticket_id}  type={t.type}  status={t.status}")
                lines.append(f"subject: {t.subject or '-'}")
                lines.append(f"added: {_dt(t.date_added)}   updated: {_dt(t.date_updated)}   closed: {_dt(t.date_closed)}")
                lines.append(f"created_by: {uname(t.created_by)}")
                lines.append(f"claimed_by: {uname(t.claimed_by)}")
                lines.append(f"closed_by: {uname(t.closed_by)}")
                msgs = (s.query(TicketMessage.author_name, TicketMessage.content)
                          .filter(TicketMessage.ticket_id == t.ticket_id)
                          .order_by(desc(TicketMessage.date_sent))
                          .limit(10).all())
                if msgs:
                    lines.append("last messages:")
                    for author, content in reversed(msgs):
                        c = (content or "").replace("\n", " ").replace("\r", " ")
                        lines.append(f"  {author or '?'}: {c[:80]}")
                return "\n".join(lines)
    except Exception as e:
        return f"Lookup error: {e}"
    return f"Unknown kind '{kind}'."


class KBAdminCommands(Extension):
    """Owner-only knowledgebase + database admin commands."""

    def __init__(self, bot):
        self.bot = bot

    async def _require_owner(self, ctx) -> bool:
        ok = await asyncio.to_thread(_is_owner_discord_id, str(ctx.author.id))
        if not ok:
            cmd = getattr(ctx, "invoke_target", None) or getattr(getattr(ctx, "command", None), "name", "") or "?"
            await asyncio.to_thread(_audit, str(ctx.author.id), "adminbot.denied", str(cmd))
            await ctx.send("This bot is owner-only.", ephemeral=True)
            return False
        return True

    # ------------------------------------------------------------------ /ask
    @slash_command(name="ask", description="Ask the DropTracker knowledgebase (RAG answer)", scopes=SCOPES)
    @slash_option(name="question", description="Your question", opt_type=OptionType.STRING, required=True)
    async def ask_cmd(self, ctx: SlashContext, question: str):
        if not await self._require_owner(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            # answer_smart routes between live-DB investigation (validated
            # read-only SQL) and knowledgebase RAG — see services/kb/investigator.py
            from services.kb.investigator import answer_smart
            from services.kb.answerer import AnswerError
        except Exception as e:
            return await ctx.send(f"KB module unavailable: {e}", ephemeral=True)
        try:
            res = await answer_smart(question)
        except AnswerError as e:
            return await ctx.send(f"Answer error: {e}", ephemeral=True)
        except Exception as e:
            return await ctx.send(f"Error answering: {e}", ephemeral=True)

        answer_text = _g(res, "answer", "") or ""
        sources = _g(res, "sources", []) or []
        src_lines = []
        for i, src in enumerate(sources, 1):
            stype = _g(src, "source_type", None) or _g(src, "type", None) or "?"
            title = _g(src, "title", None) or _g(src, "source_ref", None) or _g(src, "url", None) or "?"
            src_lines.append(f"[{i}] ({stype}) {title}")
        sources_str = "\n".join(src_lines)[:1000] or "—"

        embed = Embed(description=answer_text[:4000] or "(empty answer)", color=_EMBED_COLOR)
        if sources_str != "—":
            embed.add_field(name="Sources", value=sources_str, inline=False)
        # Owner transparency: show exactly which validated SELECTs the
        # investigator executed (read-only, LIMIT-capped by sql_guard).
        exec_sql = _g(res, "sql", []) or []
        if exec_sql:
            sql_str = "\n".join(f"• {q}" for q in exec_sql)
            embed.add_field(name="Investigation SQL", value=f"```sql\n{sql_str[:950]}\n```", inline=False)
        mode = _g(res, "mode", "kb")
        # Token usage across every CLI call this answer made (plan/refine/
        # synthesis). cost_usd is the API-EQUIVALENT price — informational
        # only, nothing is billed (subscription auth).
        usage = _g(res, "usage", {}) or {}
        _tk = lambda n: f"{n/1000:.1f}k" if n >= 1000 else str(n)  # noqa: E731
        tok_in = sum(int(usage.get(k, 0) or 0) for k in ("input_tokens", "cache_read", "cache_creation"))
        tok_out = int(usage.get("output_tokens", 0) or 0)
        usage_str = (
            f" · {_tk(tok_in)}→{_tk(tok_out)} tok · ≈${float(usage.get('cost_usd', 0.0) or 0.0):.4f}"
            f" ({int(usage.get('calls', 0) or 0)} calls, sub)"
            if usage else ""
        )
        embed.set_footer(
            text=f"mode: {mode} · {_g(res, 'retrieved', 0)} chunks · {_g(res, 'elapsed_s', '?')}s · {os.getenv('KB_CLAUDE_MODEL', 'sonnet')}{usage_str}"
        )
        await ctx.send(embed=embed, ephemeral=True)

        if len(answer_text) > 4000:
            for chunk in _split_chunks(answer_text[4000:], 1900):
                await ctx.send(chunk, ephemeral=True)

        await asyncio.to_thread(
            _audit, str(ctx.author.id), "adminbot.ask", question,
            None, {"retrieved": _g(res, "retrieved", None), "elapsed_s": _g(res, "elapsed_s", None),
                   "answer_len": len(answer_text), "sources": len(sources),
                   "mode": _g(res, "mode", None), "sql": exec_sql,
                   "usage": _g(res, "usage", None)},
        )

    # ------------------------------------------------------------ /kb-search
    @slash_command(name="kb-search", description="Hybrid search over the knowledgebase", scopes=SCOPES)
    @slash_option(name="query", description="Search terms", opt_type=OptionType.STRING, required=True)
    @slash_option(
        name="source", description="Restrict to one source type", opt_type=OptionType.STRING, required=False,
        choices=[
            SlashCommandChoice(name="ticket", value="ticket"),
            SlashCommandChoice(name="forum", value="forum"),
            SlashCommandChoice(name="chat", value="chat"),
            SlashCommandChoice(name="doc", value="doc"),
        ],
    )
    async def kb_search_cmd(self, ctx: SlashContext, query: str, source: str = None):
        if not await self._require_owner(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            from services.kb.retriever import search
        except Exception as e:
            return await ctx.send(f"KB module unavailable: {e}", ephemeral=True)
        try:
            hits = await asyncio.to_thread(search, query, 8, [source] if source else None)
        except Exception as e:
            return await ctx.send(f"Search error: {e}", ephemeral=True)

        hits = hits or []
        if not hits:
            return await ctx.send("No results.", ephemeral=True)
        lines = []
        for i, h in enumerate(hits, 1):
            stype = _g(h, "source_type", None) or "?"
            title = _g(h, "title", None) or _g(h, "source_ref", None) or "?"
            score = _g(h, "score", 0.0) or 0.0
            content = (_g(h, "content", "") or "").replace("\n", " ").replace("\r", " ")
            try:
                score_s = f"{float(score):.4f}"
            except Exception:
                score_s = str(score)
            lines.append(f"**{i}.** ({stype}) {title} — score {score_s}\n{content[:150]}")
        embed = Embed(title=f"KB search — {query[:200]}", description="\n".join(lines)[:4000], color=_EMBED_COLOR)
        await ctx.send(embed=embed, ephemeral=True)
        await asyncio.to_thread(
            _audit, str(ctx.author.id), "adminbot.kb-search", query,
            None, {"hits": len(hits), "source": source or "all"},
        )

    # ------------------------------------------------------------- /kb-stats
    @slash_command(name="kb-stats", description="Knowledgebase corpus + ingest stats", scopes=SCOPES)
    async def kb_stats_cmd(self, ctx: SlashContext):
        if not await self._require_owner(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            from services.kb.retriever import stats
        except Exception as e:
            return await ctx.send(f"KB module unavailable: {e}", ephemeral=True)
        try:
            st = await asyncio.to_thread(stats)
        except Exception as e:
            return await ctx.send(f"Stats error: {e}", ephemeral=True)
        ingest = await asyncio.to_thread(_ingest_states)

        embed = Embed(title="Knowledgebase stats", description=_render_stats(st)[:4000], color=_EMBED_COLOR)
        if ingest:
            ing_lines = [f"{ref} — {status} — {_dt(ts)}" for ref, status, ts in ingest]
            embed.add_field(name="Ingest state", value="\n".join(ing_lines)[:1000], inline=False)
        else:
            embed.add_field(name="Ingest state", value="—", inline=False)
        await ctx.send(embed=embed, ephemeral=True)
        await asyncio.to_thread(
            _audit, str(ctx.author.id), "adminbot.kb-stats", "",
            None, {"ingest_rows": len(ingest)},
        )

    # -------------------------------------------------------------- /kb-sync
    @slash_command(name="kb-sync", description="Mine a KB source then embed new chunks", scopes=SCOPES)
    @slash_option(
        name="source", description="Which source(s) to sync", opt_type=OptionType.STRING, required=True,
        choices=[
            SlashCommandChoice(name="all", value="all"),
            SlashCommandChoice(name="tickets", value="tickets"),
            SlashCommandChoice(name="docs", value="docs"),
            SlashCommandChoice(name="forums", value="forums"),
            SlashCommandChoice(name="chat", value="chat"),
        ],
    )
    async def kb_sync_cmd(self, ctx: SlashContext, source: str):
        if not await self._require_owner(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            from services.kb import miner
        except Exception as e:
            return await ctx.send(f"KB module unavailable: {e}", ephemeral=True)

        results = {}
        try:
            if source in ("all", "tickets"):
                results["tickets"] = await asyncio.to_thread(miner.mine_tickets)
            if source in ("all", "docs"):
                results["docs"] = await asyncio.to_thread(miner.mine_docs)
            if source in ("all", "forums"):
                results["forums"] = await miner.mine_forums(self.bot)
            if source in ("all", "chat"):
                ids = [x.strip() for x in os.getenv("KB_CHAT_CHANNEL_IDS", "").split(",") if x.strip()]
                if not ids:
                    if source == "chat":
                        return await ctx.send("Set KB_CHAT_CHANNEL_IDS first.", ephemeral=True)
                    results["chat"] = "skipped (KB_CHAT_CHANNEL_IDS not set)"
                else:
                    results["chat"] = await miner.mine_chat(self.bot, ids)
            results["embedded"] = await asyncio.to_thread(miner.embed_missing_chunks)
        except Exception as e:
            return await ctx.send(f"Sync error: {e}", ephemeral=True)

        body = json.dumps(results, default=str, indent=2)
        if len(body) > 3800:
            body = body[:3800] + "\n… (truncated)"
        embed = Embed(title=f"KB sync — {source}", description=f"```json\n{body}\n```", color=_EMBED_COLOR)
        await ctx.send(embed=embed, ephemeral=True)
        await asyncio.to_thread(
            _audit, str(ctx.author.id), "adminbot.kb-sync", source,
            None, {"ran": list(results.keys())},
        )

    # --------------------------------------------------------------- /lookup
    @slash_command(name="lookup", description="Look up a player/group/user/ticket in the DB", scopes=SCOPES)
    @slash_option(
        name="kind", description="What to look up", opt_type=OptionType.STRING, required=True,
        choices=[
            SlashCommandChoice(name="player", value="player"),
            SlashCommandChoice(name="group", value="group"),
            SlashCommandChoice(name="user", value="user"),
            SlashCommandChoice(name="ticket", value="ticket"),
        ],
    )
    @slash_option(name="query", description="ID or name (ticket = numeric id)", opt_type=OptionType.STRING, required=True)
    async def lookup_cmd(self, ctx: SlashContext, kind: str, query: str):
        if not await self._require_owner(ctx):
            return
        await ctx.defer(ephemeral=True)
        try:
            text = await asyncio.to_thread(_lookup_sync, kind, query)
        except Exception as e:
            return await ctx.send(f"Lookup error: {e}", ephemeral=True)
        text = (text or "(no result)")[:1850]
        await ctx.send(f"```text\n{text}\n```", ephemeral=True)
        await asyncio.to_thread(
            _audit, str(ctx.author.id), "adminbot.lookup", f"{kind}:{query}",
            None, {"chars": len(text)},
        )

    # ------------------------------------------------------------------ /sql
    @slash_command(name="sql", description="Run a read-only SQL query (gated)", scopes=SCOPES)
    @slash_option(name="query", description="SELECT / SHOW / EXPLAIN / DESCRIBE only", opt_type=OptionType.STRING, required=True)
    async def sql_cmd(self, ctx: SlashContext, query: str):
        if not await self._require_owner(ctx):
            return
        await ctx.defer(ephemeral=True)

        # Gate 1: feature flag.
        if os.getenv("KB_ALLOW_SQL", "false").lower() != "true":
            await asyncio.to_thread(
                _audit, str(ctx.author.id), "adminbot.sql", query,
                {"query": query[:2000]}, {"ok": False, "rows": 0, "reason": "disabled"},
            )
            return await ctx.send("Raw SQL is disabled (KB_ALLOW_SQL=false).", ephemeral=True)

        # Gate 2: validation.
        ok, out = validate_readonly_sql(query)
        if not ok:
            await asyncio.to_thread(
                _audit, str(ctx.author.id), "adminbot.sql", query,
                {"query": query[:2000]}, {"ok": False, "rows": 0, "reason": out},
            )
            return await ctx.send(f"Rejected: {out}", ephemeral=True)

        try:
            cols, rows = await asyncio.to_thread(run_readonly_sql, out)
        except Exception as e:
            await asyncio.to_thread(
                _audit, str(ctx.author.id), "adminbot.sql", query,
                {"query": query[:2000]}, {"ok": False, "rows": 0, "error": str(e)[:500]},
            )
            return await ctx.send(f"SQL error: {e}", ephemeral=True)

        table = _fmt_table(cols, rows)
        if len(table) > 1800:
            table = table[:1800] + "\n… (truncated)"
        body = f"```\n{table}\n```\n{len(rows)} row(s)"
        await ctx.send(body, ephemeral=True)
        await asyncio.to_thread(
            _audit, str(ctx.author.id), "adminbot.sql", query,
            {"query": query[:2000]}, {"ok": True, "rows": len(rows)},
        )


def setup(bot):
    KBAdminCommands(bot)
