"""Text chunking for the knowledgebase miner (pure functions, no I/O).

Two shapes of source material are chunked here:

- Conversations (ticket transcripts, forum threads, chat months) -> a
  chronological list of message dicts is rendered to one line per message
  ("[timestamp] author: content") and those lines are packed into
  character-bounded windows. Consecutive windows share a small tail of lines
  (``overlap_msgs``) so a chunk boundary never severs the surrounding context
  from a retrieved passage.

- Markdown docs -> the text is split at heading lines and whole sections are
  packed into windows; sections/paragraphs larger than the budget are broken
  down before packing.

Everything is measured in characters (no tokenizer dependency); the miner
derives a rough token estimate as ``len(content) // 4``. All functions return a
list of stripped, non-empty chunk strings.
"""
from __future__ import annotations

import re
from datetime import datetime


def render_message_line(author: str, ts, content: str) -> str:
    """Render one message as ``[YYYY-MM-DD HH:MM] author: content``.

    ``ts`` may be ``None`` (or otherwise non-datetime), in which case the
    timestamp field renders as ``[unknown time]``.
    """
    if isinstance(ts, datetime):
        try:
            stamp = f"[{ts:%Y-%m-%d %H:%M}]"
        except Exception:
            stamp = "[unknown time]"
    else:
        stamp = "[unknown time]"
    return f"{stamp} {author}: {content}"


def _joined_len(lines: list[str]) -> int:
    """Length of ``"\\n".join(lines)`` without building the string."""
    if not lines:
        return 0
    return sum(len(line) for line in lines) + (len(lines) - 1)


def chunk_messages(
    messages: list[dict],
    target_chars: int = 1400,
    overlap_msgs: int = 2,
) -> list[str]:
    """Pack chronological message dicts into overlapping character windows.

    ``messages`` is a chronological list of ``{'author', 'ts', 'content'}``
    dicts. Each message renders to a single line; a line longer than
    ``target_chars`` is hard-split at ``target_chars`` boundaries into several
    lines. Lines are packed greedily into windows no larger than
    ``target_chars``; when a window fills, the next window is seeded with the
    last ``overlap_msgs`` lines of the previous one so context carries across
    the boundary. Returns stripped, non-empty chunk strings.
    """
    # 1) Render to lines, hard-splitting any over-long line so no single line
    #    can ever exceed target_chars (guarantees every window fits).
    lines: list[str] = []
    for msg in messages:
        line = render_message_line(
            msg.get("author", ""), msg.get("ts"), msg.get("content", "")
        )
        if len(line) > target_chars > 0:
            for i in range(0, len(line), target_chars):
                lines.append(line[i : i + target_chars])
        else:
            lines.append(line)

    # 2) Greedy pack with a trimmed overlap seed on each boundary.
    windows: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if current and _joined_len(current + [line]) > target_chars:
            windows.append(current)
            seed = current[-overlap_msgs:] if overlap_msgs > 0 else []
            # Trim the seed from the front until the triggering line fits, so a
            # large overlap tail can never push the new window over budget (and
            # can't cause the seed to be re-emitted window after window).
            while seed and _joined_len(seed + [line]) > target_chars:
                seed = seed[1:]
            current = seed + [line]
        else:
            current.append(line)
    if current:
        windows.append(current)

    chunks: list[str] = []
    for window in windows:
        text = "\n".join(window).strip()
        if text:
            chunks.append(text)
    return chunks


def _split_sections(text: str) -> list[str]:
    """Split markdown into sections, each beginning at a heading line.

    A heading is any line that starts with ``#``. The heading stays attached to
    the body that follows it (up to the next heading). Any content preceding the
    first heading forms its own leading section.
    """
    sections: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.startswith("#"):
            if current:
                sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections


def _split_paragraphs(text: str) -> list[str]:
    """Split a section on blank lines into non-empty paragraphs."""
    parts = re.split(r"\n[ \t]*\n", text)
    return [p.strip("\n") for p in parts if p.strip()]


def chunk_markdown(text: str, target_chars: int = 1800) -> list[str]:
    """Chunk markdown by packing whole sections into character windows.

    Sections (heading + body) are packed consecutively into windows no larger
    than ``target_chars``. A section larger than the budget is broken on
    blank-line paragraphs and those are packed instead; a single paragraph still
    too large is hard-split at ``target_chars`` boundaries. Returns stripped,
    non-empty chunk strings.
    """
    # Flatten to a stream of packable units: whole sections normally, or the
    # paragraphs/hard-split pieces of an over-sized section.
    units: list[str] = []
    for section in _split_sections(text):
        if len(section) <= target_chars or target_chars <= 0:
            units.append(section)
            continue
        for para in _split_paragraphs(section):
            if len(para) <= target_chars:
                units.append(para)
            else:
                for i in range(0, len(para), target_chars):
                    units.append(para[i : i + target_chars])

    chunks: list[str] = []
    current = ""
    for unit in units:
        if not unit.strip():
            continue
        if current and len(current) + 2 + len(unit) > target_chars:
            stripped = current.strip()
            if stripped:
                chunks.append(stripped)
            current = unit
        else:
            current = f"{current}\n\n{unit}" if current else unit
    stripped = current.strip()
    if stripped:
        chunks.append(stripped)
    return chunks
