"""Answer synthesis for the DropTracker knowledgebase.

Retrieves grounding context with the hybrid retriever, then synthesizes an
answer by piping a prompt to the Claude Code CLI (``claude -p``) running under
the machine's local subscription session. ANTHROPIC_API_KEY is stripped from
the child environment so synthesis can NEVER fall back to metered API billing
— this is the zero-API-cost design.

Public API:
    build_prompt(question, hits) -> str
    answer(question, top_k=8, source_types=None) -> dict   (coroutine)
"""

# Import the retriever first: it imports db.models, whose package __init__ runs
# load_dotenv(), so the CLAUDE_* environment below is populated before we read
# it. (search() is our own retriever function.)
from services.kb.retriever import search

import asyncio
import json
import os
import time

_CLAUDE = os.getenv("CLAUDE_CLI_PATH", "claude")
_MODEL = os.getenv("KB_CLAUDE_MODEL", "sonnet")
_TIMEOUT = int(os.getenv("KB_CLAUDE_TIMEOUT", "180"))
_sem = asyncio.Semaphore(1)  # one synthesis at a time (owner-only usage)

# Prompt budgeting.
_CHUNK_CHAR_CAP = 2000
_CONTEXT_CHAR_CAP = 16000

# The persona moved out of the piped prompt and into --system-prompt, which
# REPLACES Claude Code's default system prompt. That default is built for an
# interactive coding agent (tool docs, harness rules, skill catalogue, per-machine
# context) and cost ~9.5k input tokens on every call — for a text-only synthesis
# that never uses a tool, all of it was dead weight. Measured on this host:
#   before: input 1 + cache_creation 6229 + cache_read 3289  (~9.5k)
#   after:  input ~184                                       (~98% less)
# /ask makes 3+ CLI calls per question, so this is ~28k tokens saved per question.
_SYSTEM_PROMPT = (
    "You are the internal knowledgebase assistant for DropTracker, an Old School "
    "RuneScape loot/achievement tracking platform (Python backend, Discord bots, "
    "RuneLite plugin, MariaDB, Redis). You answer the project owner's operational "
    "questions. Ground every claim in the context you are given; when it is "
    "insufficient, say so and name exactly what is missing rather than guessing. "
    "Cite excerpt numbers like [1] where you rely on them. Be concise and concrete: "
    "answers are rendered into a Discord embed, so lead with the answer, prefer "
    "short lines, and include ids alongside names."
)

_HEADER = (
    "Answer the operator's question using ONLY the context excerpts below (mined\n"
    "from support tickets, Discord forums/chat, and project docs)."
)


class AnswerError(RuntimeError):
    pass


def _zero_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "cost_usd": 0.0,
        "calls": 0,
    }


def acc_usage(total: dict, meta: dict) -> dict:
    """Accumulate one call's usage meta into a running total (in place)."""
    for k in ("input_tokens", "output_tokens", "cache_read", "cache_creation", "calls"):
        total[k] = total.get(k, 0) + int(meta.get(k, 0) or 0)
    total["cost_usd"] = round(total.get("cost_usd", 0.0) + float(meta.get("cost_usd", 0.0) or 0.0), 6)
    return total


async def _run_claude_json(
    prompt: str,
    system_prompt: str | None = None,
    json_schema: dict | None = None,
) -> tuple[str, dict]:
    """Run one ``claude -p`` synthesis. Returns (text, usage_meta).

    Uses ``--output-format json`` so each call reports token usage and the
    API-equivalent cost (``total_cost_usd`` — informational only: subscription
    auth means nothing is actually billed). ANTHROPIC_API_KEY is removed from
    the child env so the CLI uses subscription auth only.

    Everything the default Claude Code harness would inject is stripped, because
    a text-only synthesis call can use none of it:
      ``--tools ""``            no tools, so no tool schemas in the prompt
      ``--system-prompt``       replaces the (large) default system prompt
      ``--strict-mcp-config``   ignores the machine's MCP servers (the claude.ai
                                ones are unauthenticated here — pure overhead)
      ``--disable-slash-commands``  drops the skill catalogue
      ``--setting-sources ""``  skips user/project/local settings discovery

    ``json_schema`` turns on the CLI's structured-output validation, so callers
    that need machine-readable output get schema-valid JSON instead of parsing
    prose and silently falling back when the model wraps it in prose or fences.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = [
        _CLAUDE, "-p",
        "--output-format", "json",
        "--model", _MODEL,
        "--tools", "",
        "--system-prompt", system_prompt or _SYSTEM_PROMPT,
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources", "",
    ]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/store/droptracker/disc",
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout=_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise AnswerError(f"claude timed out after {_TIMEOUT}s")
    if proc.returncode != 0:
        raise AnswerError(
            f"claude exited {proc.returncode}: {err.decode(errors='replace')[-400:]}"
        )
    raw = out.decode("utf-8", errors="replace").strip()
    meta = _zero_usage()
    meta["calls"] = 1
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Unexpected non-JSON (e.g. older CLI): degrade gracefully to raw text.
        return raw, meta
    u = data.get("usage") or {}
    meta["input_tokens"] = int(u.get("input_tokens") or 0)
    meta["output_tokens"] = int(u.get("output_tokens") or 0)
    meta["cache_read"] = int(u.get("cache_read_input_tokens") or 0)
    meta["cache_creation"] = int(u.get("cache_creation_input_tokens") or 0)
    meta["cost_usd"] = float(data.get("total_cost_usd") or 0.0)
    text = (data.get("result") or "").strip()
    if data.get("is_error"):
        raise AnswerError(f"claude reported an error: {text[:300]}")
    return text, meta


async def _run_claude(prompt: str) -> str:
    """Text-only compatibility wrapper around :func:`_run_claude_json`."""
    text, _meta = await _run_claude_json(prompt)
    return text


def _select_blocks(hits: list[dict]) -> list[tuple[int, dict, str]]:
    """Number, truncate, and budget the context blocks.

    Hits arrive best-first. Each chunk contributes at most _CHUNK_CHAR_CAP
    chars; total context is capped at ~_CONTEXT_CHAR_CAP chars, dropping the
    lowest-ranked overflow. Returns (excerpt_number, hit, rendered_block) for
    every included hit, in order.
    """
    selected: list[tuple[int, dict, str]] = []
    total = 0
    for h in hits:
        content = (h.get("content") or "").strip()
        if len(content) > _CHUNK_CHAR_CAP:
            content = content[:_CHUNK_CHAR_CAP].rstrip() + " …"
        title = (
            h.get("title") or h.get("source_ref") or h.get("source_type") or ""
        ).strip()
        n = len(selected) + 1
        label = f"[{n}] ({h.get('source_type')}) {title}".rstrip()
        block = f"{label}\n{content}"
        # +2 accounts for the blank line joining blocks. Always keep at least
        # the first (highest-ranked) block even if it alone is large.
        if selected and total + len(block) + 2 > _CONTEXT_CHAR_CAP:
            break
        selected.append((n, h, block))
        total += len(block) + 2
    return selected


def build_prompt(question: str, hits: list[dict]) -> str:
    """Assemble the grounded synthesis prompt: header, context, question."""
    context = "\n\n".join(block for (_n, _h, block) in _select_blocks(hits))
    return (
        f"{_HEADER}\n\n"
        "=== CONTEXT ===\n"
        f"{context}\n\n"
        "=== QUESTION ===\n"
        f"{question}\n\n"
        "Answer directly as plain text. Do not attempt to use tools."
    )


async def answer(
    question: str, top_k: int = 8, source_types: list[str] | None = None
) -> dict:
    """Retrieve context and synthesize an answer via the Claude Code CLI.

    Returns {"answer", "sources", "retrieved", "elapsed_s"}. ``sources`` is
    deduped per document, in first-appearance order:
    [{"n", "source_type", "title", "source_ref", "url"}].
    """
    start = time.time()
    async with _sem:
        hits = await asyncio.to_thread(search, question, top_k, source_types)
        if not hits:
            return {
                "answer": "The knowledgebase has no matching content for that question yet.",
                "sources": [],
                "retrieved": 0,
                "elapsed_s": round(time.time() - start, 3),
                "usage": _zero_usage(),
            }

        selected = _select_blocks(hits)
        prompt = build_prompt(question, hits)

        usage = _zero_usage()
        out, meta = await _run_claude_json(prompt)
        acc_usage(usage, meta)
        if not out:
            # Retry once (transient robustness) with the identical command.
            out, meta = await _run_claude_json(prompt)
            acc_usage(usage, meta)
        if not out:
            raise AnswerError("claude returned empty output twice")

        sources: list[dict] = []
        seen: set = set()
        for n, h, _block in selected:
            doc_id = h.get("document_id")
            if doc_id in seen:
                continue
            seen.add(doc_id)
            sources.append(
                {
                    "n": n,
                    "source_type": h.get("source_type"),
                    "title": h.get("title"),
                    "source_ref": h.get("source_ref"),
                    "url": h.get("url"),
                }
            )

        return {
            "answer": out,
            "sources": sources,
            "retrieved": len(hits),
            "elapsed_s": round(time.time() - start, 3),
            "usage": usage,
        }
