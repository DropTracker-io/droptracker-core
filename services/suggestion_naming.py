"""Forum thread names for suggestions and bug reports.

Threads are named ``<emoji> #<id> · <title>`` so a post in Discord can be
matched to its ``suggestions`` row by id. Pure helpers (no Discord or DB
imports) so both the outbox (web-created posts) and the Discord mirror
(Discord-created posts) share one format.
"""
import re

THREAD_NAME_MAX = 100  # Discord's thread-name cap

KIND_EMOJI = {"suggestion": "\N{ELECTRIC LIGHT BULB}", "bug": "\N{BUG}"}
KIND_LABEL = {"suggestion": "suggestion", "bug": "bug report"}

_ALL_EMOJI = "|".join(re.escape(e) for e in KIND_EMOJI.values())
# Only our own "#<digits> · " marker (optionally behind a kind emoji) is
# stripped, so a user title like "#5 broken" is never touched.
_NUMBER_PREFIX = re.compile(rf"^(?:(?:{_ALL_EMOJI})\s*)?#\d+\s*·\s*")


def strip_number_prefix(name: str) -> str:
    """Remove our ``[emoji ]#<id> · `` prefix from a thread name."""
    return _NUMBER_PREFIX.sub("", name or "", count=1).strip()


def thread_name(kind: str, suggestion_id: int, title: str) -> str:
    """``<emoji> #<id> · <title>``, capped at 100 characters by shortening
    the title; the id prefix is always kept."""
    emoji = KIND_EMOJI.get(kind, "")
    title = strip_number_prefix(title)
    if emoji and title.startswith(emoji):
        title = title[len(emoji):].lstrip()
    prefix = f"{emoji} " if emoji else ""
    prefix += f"#{int(suggestion_id)} \N{MIDDLE DOT} "
    title = title or "Untitled"
    return prefix + title[: max(THREAD_NAME_MAX - len(prefix), 0)].rstrip()


def is_numbered(name: str, suggestion_id: int) -> bool:
    """Whether ``name`` already carries this suggestion's id marker."""
    pattern = rf"^(?:(?:{_ALL_EMOJI})\s*)?#{int(suggestion_id)}\s*·"
    return re.match(pattern, name or "") is not None


def fallback_note(kind: str, suggestion_id: int) -> str:
    """Subtext line posted in a thread that could not be renamed."""
    return f"-# Tracked as {KIND_LABEL.get(kind, 'suggestion')} #{int(suggestion_id)}"
