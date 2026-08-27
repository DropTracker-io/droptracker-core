"""Whether this instance may call third-party APIs (Wise Old Man, OSRS Wiki).

DropTracker has had repeated contact from both the WOM and OSRS Wiki maintainers
about request volume — the wiki went as far as blocklisting our User-Agent in
2026-08 (see utils/wiki_ua or the wiki-UA incident notes), which silently
fail-opened the high-value drop check for everyone. Those relationships are the
constraint this module exists to protect.

A dev instance is the worst offender by construction: it runs on a restored
production dump, so its caches are cold while its data is full of real players
and groups it will happily re-resolve. Left alone it doubles our footprint
against both services while producing nothing anybody needs.

So **dev is silent by default**. Production is unaffected — `is_dev_mode()` is
false there and this always returns True. Set ``DEV_ALLOW_EXTERNAL_APIS=true``
on a dev box for the specific session where a real lookup is genuinely needed
(seeding a new player, verifying a fix against live data), and unset it after.

Enforced at the transport layer rather than at call sites: the WOM shared rate
limiter (`utils/wiseoldman._SharedRateLimiter.wait`) already returns False for
"could not make this call" and all 17 of its callers handle that, so declining
there costs nothing and cannot be forgotten by a new caller.
"""

import os


def _flag(name: str) -> bool:
    """Read a boolean env var, tolerant of the quoting this repo's .env uses."""
    return (os.getenv(name) or "").strip().strip('"').strip("'").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def external_apis_allowed() -> bool:
    """False on a dev instance that has not opted in. Always True in production."""
    from utils.dev_guild_guard import is_dev_mode

    if not is_dev_mode():
        return True
    return _flag("DEV_ALLOW_EXTERNAL_APIS")


def describe() -> str:
    """One-line summary for a startup log line."""
    from utils.dev_guild_guard import is_dev_mode

    if not is_dev_mode():
        return "external APIs: unrestricted (production)"
    if _flag("DEV_ALLOW_EXTERNAL_APIS"):
        return ("external APIs: ALLOWED on this dev instance "
                "(DEV_ALLOW_EXTERNAL_APIS is set — unset it when you are done)")
    return "external APIs: blocked (dev instance; set DEV_ALLOW_EXTERNAL_APIS to override)"
