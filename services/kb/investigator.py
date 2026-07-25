"""Agentic read-only DB investigation for the admin bot's ``/ask``.

Two-stage flow, both stages on the Claude Code CLI (subscription auth, zero
API cost — same ``_run_claude`` as :mod:`services.kb.answerer`):

1. **Plan** — the model sees the question plus a curated schema card (table
   columns, FKs, domain notes) and returns strict JSON choosing a mode:
   ``kb`` (conversational/how-it-works → classic RAG), ``db`` (live-record
   questions → up to 3 SELECTs), or ``both``. Mined Discord content is
   deliberately NOT shown to this stage, so untrusted chat text can never
   steer SQL generation — only the owner's question and the schema card can.
2. **Execute + Answer** — every generated query must pass
   :func:`services.kb.sql_guard.validate_readonly_sql` (the same validator
   behind ``/sql``) and runs via ``run_readonly_sql`` (AUTOCOMMIT,
   ``max_statement_time=10``, session READ ONLY, ≤50 rows). Results (including
   empty sets and per-query errors) are handed to a second model call that
   writes the final grounded answer.

This module never writes to the database and never bypasses the guard; the
``KB_ALLOW_SQL`` flag is unrelated (it gates the owner-typed ``/sql`` surface
only).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

from services.kb.sql_guard import validate_readonly_sql, run_readonly_sql
from services.kb.answerer import (
    _run_claude_json,
    _sem,
    _zero_usage,
    acc_usage,
    answer as _kb_answer,
    AnswerError,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Schema card
# --------------------------------------------------------------------------- #

# Allowlisted tables the planner may query. Kept curated (not the full ~80-table
# metadata) so the prompt stays small and the model isn't tempted into obscure
# corners. Extend deliberately.
_CARD_TABLES = [
    "groups",
    "guilds",
    "users",
    "group_admins",
    "group_configurations",
    "user_group_association",
    "players",
    "drops",
    "tickets",
    "ticket_messages",
    "group_subscriptions",
    "subscription_tiers",
    "player_points",
    "items",
    "npc_list",
]

# Curated operational knowledge the raw schema can't express. This is what
# makes answers *truthful* (e.g. admin rights that live Discord-side).
_DOMAIN_NOTES = """
Domain notes (authoritative):
- groups: group_id (PK), group_name, wom_id (WiseOldMan group), guild_id
  (Discord guild snowflake, STRING). guilds maps guild_id -> group_id.
- "Who administers group X" resolves in THIS order:
  1) group_admins rows (user_id -> users; role 'owner'/'admin');
  2) group_configurations row (group_id, config_key='authed_users') — JSON
     array of Discord id strings;
  3) anyone with the Discord "Manage Server" permission in the linked guild —
     NOT stored in this database (if 1 and 2 are empty, say exactly that:
     administration is Discord-permission-based for that group);
  4) site superadmins implicitly administer everything.
- users: user_id (PK), discord_id (STRING snowflake), username. players link
  to users via players.user_id. user_group_association links user_id<->group_id.
- group_configurations is key/value per group (config_key, config_value) —
  long values (e.g. the authed_users JSON list) live in the long_value column,
  so SELECT both config_value AND long_value.
- drops has MILLIONS of rows: always filter (player_id / npc_id / date_added)
  and always LIMIT. Never aggregate over the whole table without a filter.
- All Discord ids are stored as strings — quote them in SQL.
- Dates are DATETIME columns; compare with 'YYYY-MM-DD' literals.

Name -> id resolution (drops stores ONLY numeric ids, never names):
- Item names live in `items` (item_id, item_name); NPC names in `npc_list`
  (npc_id, npc_name). drops.item_id / drops.npc_id are the join keys.
- ONE item name can map to MULTIPLE item_ids (noted/stacked/variant rows). Never
  resolve a name to a single id with LIMIT 1 — that silently loses drops.
  Resolve the whole set and match against all of them, e.g.:
    SELECT DISTINCT n.npc_name, COUNT(*) c FROM drops d
      JOIN npc_list n ON n.npc_id = d.npc_id
     WHERE d.item_id IN (SELECT item_id FROM items WHERE item_name = 'Vial of blood')
     GROUP BY n.npc_name ORDER BY c DESC LIMIT 50
- Prefer resolving names inside a subquery like that (one round-trip) over
  spending a separate query on the lookup.
- Match item/npc names case-insensitively and exactly where possible; fall back
  to LIKE '%name%' only when an exact match returns nothing.
""".strip()

_schema_card_cache: str | None = None


def build_schema_card() -> str:
    """Compact `table: col:TYPE, ... [FK col->table.col]` card from live ORM
    metadata, restricted to the allowlist. Cached per process."""
    global _schema_card_cache
    if _schema_card_cache is not None:
        return _schema_card_cache
    import db.models  # ensure all models are registered on Base.metadata  # noqa: F401
    from db.models.base import Base

    lines: list[str] = []
    missing: list[str] = []
    for tname in _CARD_TABLES:
        table = Base.metadata.tables.get(tname)
        if table is None:
            # A typo here is invisible at runtime but silently blinds the
            # planner to a whole table (this is exactly how `item_list` — real
            # name `items` — went unnoticed and made every item-name question
            # fail). Complain loudly instead of dropping it on the floor.
            missing.append(tname)
            continue
        cols = []
        fks = []
        for col in table.columns:
            try:
                ctype = str(col.type).split("(")[0]
            except Exception:
                ctype = "?"
            cols.append(f"{col.name}:{ctype}")
            for fk in col.foreign_keys:
                fks.append(f"{col.name}->{fk.target_fullname}")
        line = f"- {tname}({', '.join(cols)})"
        if fks:
            line += f"  [FK {', '.join(fks)}]"
        lines.append(line)
    if missing:
        logger.warning(
            "KB schema card: %d allowlisted table(s) not found in ORM metadata "
            "and omitted from the planner prompt: %s",
            len(missing), ", ".join(missing),
        )
    _schema_card_cache = "\n".join(lines)
    return _schema_card_cache


# --------------------------------------------------------------------------- #
# Stage 1: plan
# --------------------------------------------------------------------------- #

_PLAN_TEMPLATE = """You are the query planner for DropTracker's owner-only admin bot
(DropTracker = Old School RuneScape loot/achievement tracking platform; MariaDB).
Decide how to answer the operator's question and, if database records are
needed, write the SQL.

MODES
- "kb": how-it-works / support-history / documentation questions -> answered
  from the mined knowledgebase, no SQL.
- "db": questions about specific live records (a group, player, user, ticket,
  drop, configuration, counts/rankings) -> up to 3 read-only SELECT queries.
- "both": needs records AND context/history.

SCHEMA
{schema}

{notes}

RULES for queries: plain SELECT only (no INSERT/UPDATE/DELETE/DDL, no
semicolons, no comments); single statement each; ALWAYS include LIMIT (<=50);
at most 3 queries; today's date is {today}.

QUESTION: {question}

Respond with ONLY a JSON object, no markdown fences, exactly this shape:
{{"mode": "kb"|"db"|"both", "queries": [{{"purpose": "...", "sql": "SELECT ..."}}]}}
("queries" must be [] when mode is "kb".)"""


# The planner runs with its own replacement system prompt (not Claude Code's
# default) — see answerer._run_claude_json. Kept separate from the answering
# persona so the SQL-writing stage never inherits "be helpful, cite excerpts"
# framing, and so mined Discord content stays out of this stage entirely.
_PLANNER_SYSTEM = (
    "You are a MariaDB query planner for DropTracker, an Old School RuneScape "
    "loot-tracking platform. You translate an operator's question into a small "
    "set of read-only SELECT statements against a known schema. You output only "
    "JSON matching the requested shape — never prose, never markdown fences. "
    "Correctness over cleverness: prefer one precise query with the right joins "
    "over several vague ones."
)

_QUERIES_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "purpose": {"type": "string"},
            "sql": {"type": "string"},
        },
        "required": ["purpose", "sql"],
        "additionalProperties": False,
    },
}

# --json-schema makes the CLI validate structured output, so the tolerant
# text parsing below is now a safety net rather than the primary path. That
# matters: the old failure mode was a malformed plan silently degrading to
# mode="kb", i.e. a live-records question answered from mined chat instead.
_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["kb", "db", "both"]},
        "queries": _QUERIES_SCHEMA,
    },
    "required": ["mode", "queries"],
    "additionalProperties": False,
}

_REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean"},
        "queries": _QUERIES_SCHEMA,
    },
    "required": ["done"],
    "additionalProperties": False,
}

# How many follow-up query rounds the investigator may run after the initial
# plan. Multi-hop questions ("which NPCs dropped X", "who administers Y")
# routinely need 2 hops; the old hard-coded single round capped them short.
_MAX_ROUNDS = max(0, int(os.getenv("KB_MAX_ROUNDS", "3")))


def _parse_plan(raw: str) -> dict:
    """Best-effort strict-JSON parse; falls back to kb mode."""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        s = s[start : end + 1]
    try:
        plan = json.loads(s)
        mode = plan.get("mode")
        queries = plan.get("queries") or []
        if mode not in ("kb", "db", "both") or not isinstance(queries, list):
            raise ValueError("bad shape")
        return {"mode": mode, "queries": queries[:3]}
    except Exception as e:
        # Should be unreachable now that the plan call uses --json-schema; if it
        # fires, a live-records question is about to be answered from mined chat
        # instead, so make it visible rather than silently degrading.
        logger.warning("KB planner returned unparseable output (%s); falling back to kb mode. Raw: %.300s", e, raw)
        return {"mode": "kb", "queries": []}


# --------------------------------------------------------------------------- #
# Stage 2: execute + answer
# --------------------------------------------------------------------------- #


def _execute_plan(queries: list) -> list[dict]:
    """Validate + run each planned query. Returns per-query result dicts;
    validation failures and runtime errors are recorded, never raised."""
    results = []
    for q in queries:
        sql = str((q or {}).get("sql", "")).strip()
        purpose = str((q or {}).get("purpose", ""))[:200]
        ok, out = validate_readonly_sql(sql)
        if not ok:
            results.append({"purpose": purpose, "sql": sql, "error": f"rejected: {out}"})
            continue
        try:
            cols, rows = run_readonly_sql(out)
            results.append(
                {
                    "purpose": purpose,
                    "sql": out,
                    "columns": cols,
                    "rows": [[str(c)[:600] for c in row] for row in rows],
                }
            )
        except Exception as e:  # surface DB errors to the answer stage
            results.append({"purpose": purpose, "sql": out, "error": str(e)[:300]})
    return results


_REFINE_TEMPLATE = """You are investigating a question against DropTracker's MariaDB.
This is follow-up round {round} of at most {max_rounds}. Queries run so far:

{db_results}

{notes}

QUESTION: {question}

Decide whether the data above already answers the question FULLY.
- If yes, respond with {{"done": true, "queries": []}}.
- If one more round would materially improve it, respond with
  {{"done": false, "queries": [{{"purpose": "...", "sql": "SELECT ..."}}]}}.

Ask for more queries when: you resolved ids but still need their names (or vice
versa); a query returned an error you can correct; a result is empty ONLY because
the filter was too strict (e.g. exact name match -> try LIKE). Do NOT ask for
more when the answer is simply "no such records exist" — an empty result is a
valid, meaningful answer. Max 2 queries; plain single-statement SELECTs with
LIMIT <=50; no semicolons."""


_ANSWER_TEMPLATE = """You are the internal assistant for DropTracker (OSRS loot/achievement
tracking platform). Answer the operator's question from the investigation data
below. Ground every claim in that data.

- Empty result sets are meaningful — state plainly what the absence implies
  (see domain notes), do not guess records into existence.
- If knowledgebase excerpts are provided, cite them like [1] where used.
- Be concise and concrete. Include ids alongside names where helpful.

{notes}

=== DATABASE RESULTS ===
{db_results}

{kb_block}=== QUESTION ===
{question}

Answer directly as plain text. Do not attempt to use tools."""


def _format_db_results(results: list[dict]) -> str:
    if not results:
        return "(no queries were run)"
    parts = []
    for i, r in enumerate(results, 1):
        head = f"Query {i}: {r.get('purpose') or '(no purpose given)'}\nSQL: {r.get('sql')}"
        if "error" in r:
            parts.append(f"{head}\nERROR: {r['error']}")
        else:
            rows = r.get("rows") or []
            body = json.dumps({"columns": r.get("columns"), "rows": rows}, default=str)
            if len(body) > 4000:
                body = body[:4000] + "…(truncated)"
            parts.append(f"{head}\nRESULT ({len(rows)} rows): {body}")
    return "\n\n".join(parts)


async def answer_smart(question: str, top_k: int = 4) -> dict:
    """Route a natural-language question across DB + KB and answer it.

    Returns the classic answerer dict plus ``mode`` and ``sql`` (the executed,
    validator-sanitized statements) so the caller can display/audit them."""
    t0 = time.monotonic()
    usage = _zero_usage()

    plan_prompt = _PLAN_TEMPLATE.format(
        schema=build_schema_card(),
        notes=_DOMAIN_NOTES,
        today=datetime.now().strftime("%Y-%m-%d"),
        question=question,
    )
    async with _sem:
        plan_raw, meta = await _run_claude_json(
            plan_prompt, system_prompt=_PLANNER_SYSTEM, json_schema=_PLAN_SCHEMA
        )
    acc_usage(usage, meta)
    plan = _parse_plan(plan_raw)

    if plan["mode"] == "kb":
        res = await _kb_answer(question, top_k=8)
        acc_usage(usage, res.get("usage") or {})
        res["usage"] = usage  # planner call + synthesis call combined
        res["mode"] = "kb"
        res["sql"] = []
        return res

    db_results = await asyncio.to_thread(_execute_plan, plan["queries"])

    # Refinement rounds: let the model chase follow-ups (resolve ids it found to
    # names, correct a query that errored, loosen a filter that matched nothing)
    # until it says it is done or the round budget runs out. Multi-hop questions
    # need more than the single round this used to allow; each round is now cheap
    # (~200 prompt tokens of overhead instead of ~9.5k) so the budget can be real.
    rounds_used = 0
    for rnd in range(1, _MAX_ROUNDS + 1):
        if not db_results:
            break
        refine_prompt = _REFINE_TEMPLATE.format(
            db_results=_format_db_results(db_results),
            notes=_DOMAIN_NOTES,
            question=question,
            round=rnd,
            max_rounds=_MAX_ROUNDS,
        )
        async with _sem:
            refine_raw, meta = await _run_claude_json(
                refine_prompt, system_prompt=_PLANNER_SYSTEM, json_schema=_REFINE_SCHEMA
            )
        acc_usage(usage, meta)
        rounds_used = rnd
        try:
            r = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", refine_raw.strip()))
            extra = (r.get("queries") or [])[:2] if isinstance(r, dict) and not r.get("done") else []
        except Exception:
            logger.warning("KB refine round %d returned unparseable output: %.200s", rnd, refine_raw)
            extra = []
        if not extra:
            break
        db_results += await asyncio.to_thread(_execute_plan, extra)

    kb_block = ""
    kb_sources: list[dict] = []
    retrieved = 0
    if plan["mode"] == "both":
        try:
            from services.kb.retriever import search

            hits = await asyncio.to_thread(search, question, top_k, None)
            retrieved = len(hits)
            if hits:
                blocks = []
                seen_docs: dict[int, int] = {}
                for h in hits:
                    n = len(blocks) + 1
                    blocks.append(
                        f"[{n}] ({h['source_type']}) {h.get('title') or h['source_ref']}\n"
                        + (h["content"][:1500])
                    )
                    if h["document_id"] not in seen_docs:
                        seen_docs[h["document_id"]] = n
                        kb_sources.append(
                            {
                                "n": n,
                                "source_type": h["source_type"],
                                "title": h.get("title"),
                                "source_ref": h["source_ref"],
                                "url": h.get("url"),
                            }
                        )
                kb_block = "=== KNOWLEDGEBASE EXCERPTS ===\n" + "\n\n".join(blocks) + "\n\n"
        except Exception:
            kb_block = ""  # KB trouble must not sink a DB answer

    answer_prompt = _ANSWER_TEMPLATE.format(
        notes=_DOMAIN_NOTES,
        db_results=_format_db_results(db_results),
        kb_block=kb_block,
        question=question,
    )
    async with _sem:
        text_out, meta = await _run_claude_json(answer_prompt)
    acc_usage(usage, meta)
    if not text_out:
        raise AnswerError("empty answer from claude (investigation stage)")

    return {
        "answer": text_out,
        "sources": kb_sources,
        "retrieved": retrieved,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "mode": plan["mode"],
        "sql": [r.get("sql") for r in db_results if r.get("sql")],
        "rounds": rounds_used,
        "usage": usage,
    }
