"""Canonicalization of combat achievement task names.

A CA submission is identified by its task *name* — ``ca_processor`` looks up an
existing completion with ``task_name == task_name`` — so the name is effectively
a primary key, and anything that perturbs it splits one task into two. Jagex
controls that string and changes it without notice, so it is folded to a
canonical form here, once, before the processor sees it.

Two folds, in order:

**1. Strip client markup.** The completion message is chat text and arrives with
the client's own formatting tokens embedded. On 2026-08-26 Jagex began wrapping
the task name in ``@ach_comp@`` (the token that makes it click-through in game),
and 2,921 completions landed as ``@ach_comp@Smite Fight`` — a name no previous
row had, so every one was a "new" task: a fresh row, a fresh notification, and a
Discord message showing the raw token. The tokens are markup, not text; the
game client consumes them and the player never sees them, so stripping them is
what makes our copy match what the player read. No real task name contains
``@`` or ``<`` (checked against all 655 on the wiki), which is why the patterns
here can be broad within this one field and must not be reused on free text.

**2. Snap to the known catalog.** :func:`resolve_task_name` matches the cleaned
name against the cache-derived task registry in the plugin manifest, then — on a
miss — against the wiki's ``combat_achievement`` bucket. A match returns the
catalogued spelling, so case and spacing drift can never fork a task's history.

**Resolution never rejects.** A name neither source recognizes is still
recorded, flagged ``unverified``. The catalog is rebuilt from the game cache by
hand after an update (``scripts/build_manifest.py``), so it lags every content
release: on the day this module was written it held 646 tasks against the game's
655, and rejecting the difference would have discarded 2,243 genuine completions
of that week's new content. The wiki closes most of that window but not all of
it — it is edited by volunteers after the update lands, not before. Validation
here buys canonical spelling and a signal that something is wrong; it is
deliberately not a gate, for the same reason high-value drop verification
fails open.

Matching is exact-or-normalized only, never fuzzy. "Perfect Royal Titan" and
"Perfect Royal Titans" are one task renamed, but "Royal Titan Adept" and "Royal
Titan Champion" are two tasks that differ by as little; nothing here can tell
those cases apart, so a near miss stays unverified and visible rather than being
silently snapped onto its neighbour.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

__all__ = [
    "ResolvedTask",
    "catalog_index",
    "clean_task_name",
    "invalidate_caches",
    "resolve_task_name",
    "task_key",
]

#: Manifest section holding the cache-derived task registry, and the Redis key
#: the wiki's name list is cached under. Both are read-only here — the registry
#: is written by ``scripts/build_manifest.py``.
MANIFEST_SECTION = "combat_achievement_tasks"
WIKI_CACHE_KEY = "wiki:ca_tasks:v1"

#: The wiki's task list changes only on a game update, and a full fetch is two
#: paginated api.php calls. Seven days matches the drop-source cache in
#: ``osrs_api/semantic.py`` and for the same reason: the cache *is* our rate
#: limiting, since nothing in ``osrs_api`` throttles.
WIKI_CACHE_TTL_SECONDS = 7 * 24 * 3600

#: Process-local TTL on the decoded catalog, so a manifest rebuild propagates
#: without restarting the consumer. Mirrors ``web_api/routes/player_state.py``.
_CATALOG_TTL_SECONDS = 300

#: Floor on the interval between live api.php fetches, whatever the reason.
#:
#: It has to bound two very different situations, which is why it gates the
#: fetch rather than the result. A name missing from an already-cached list
#: must trigger a re-fetch — a task released since that list was built is
#: indistinguishable from junk otherwise, and waiting out the seven-day TTL to
#: find out is far too slow. But a name that is simply junk looks identical, so
#: without a floor one bad submitter would pull the whole table per submission.
#: The same floor covers a wiki outage: a failed fetch must not be retried by
#: the next submission, which is how the 2026-08 UA blocklisting turned into
#: sustained request volume against a host already refusing us.
WIKI_REFETCH_MIN_SECONDS = 30 * 60

#: Ceiling on a whole fetch (both pages plus the courtesy gap between them),
#: because this runs on the intake hot path. Generous against a healthy wiki —
#: a real fetch takes ~3s — and short enough that a stalled one costs a
#: submission its verification rather than costing a worker minutes.
WIKI_FETCH_TIMEOUT_SECONDS = 20

#: ``@ach_comp@`` and the older three-letter colour codes (``@red@``, ``@or1@``).
#: Bounded length so a stray pair of ``@`` around a sentence cannot eat it.
_CHAT_TOKEN_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{0,15}@")
#: ``<col=ff0000>``, ``<img=41>``, ``<br>`` — the angle-bracket markup family.
_TAG_RE = re.compile(r"<[^<>]*>")
#: The plugin already strips this, but manual and broadcast paths do not. The
#: optional trailing period matters: the suffix sits *inside* the sentence
#: ("... Whack-a-Mole (3 points)."), so anchoring on a bare end-of-string never
#: matches when the full sentence tail is passed in.
_POINTS_SUFFIX_RE = re.compile(r"\s*\(\s*\d+\s*points?\s*\)\s*\.?\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
#: Everything that is not a letter or a digit, for the normalized match key.
#: Task names differ by hyphen and apostrophe placement ("Speed-Chaser" vs
#: "Speed Chaser", "You're a wizard"), and those are spelling, not identity.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_catalog_cache: tuple[float, dict[str, str]] | None = None
_wiki_cache: tuple[float, dict[str, str]] | None = None
_wiki_retry_after: float = 0.0


def clean_task_name(raw) -> str:
    """One task name with client markup and the points suffix removed.

    Safe on already-clean input and on anything non-string (returns ``""``), so
    callers can apply it unconditionally.
    """
    if raw is None:
        return ""
    text = _CHAT_TOKEN_RE.sub("", str(raw))
    text = _TAG_RE.sub("", text)
    text = text.replace(" ", " ")
    text = _WS_RE.sub(" ", text).strip()
    # NOT followed by a general trailing-period strip. Four real tasks end in
    # one — "Back in My Day...", "From Dusk...", "Shadows Move...", "Maybe I'm
    # the boss." — and 1,043 stored rows carry those names, so stripping it
    # would fork every one of them. The sentence's own final period is already
    # consumed by the plugin's capture and by _POINTS_SUFFIX_RE, which is why
    # nothing here needs to.
    return _POINTS_SUFFIX_RE.sub("", text).strip()


def task_key(name) -> str:
    """Normalized match key: lowercase, alphanumerics only.

    Used only to find a catalogued spelling for a name we already have. It
    deliberately does not stem or drop words, so it can fold punctuation drift
    without ever folding two distinct tasks together.
    """
    return _NON_ALNUM_RE.sub("", str(name or "").lower())


@dataclass(frozen=True)
class ResolvedTask:
    """The name to store, and where it was confirmed.

    ``source`` is ``catalog`` (the game cache registry), ``wiki``, or
    ``unverified``. Only ``unverified`` is actionable: it means either junk was
    submitted or the game shipped a task our registry has not been rebuilt for.
    ``name`` is always usable regardless — see the module docstring.
    """

    name: str
    source: str
    raw: str

    @property
    def verified(self) -> bool:
        return self.source != "unverified"

    @property
    def cleaned(self) -> bool:
        """Whether stripping markup actually changed the submitted name."""
        return self.raw.strip() != self.name


def invalidate_caches() -> None:
    """Drop the process-local catalog and wiki caches (tests, scripts)."""
    global _catalog_cache, _wiki_cache, _wiki_retry_after
    _catalog_cache = None
    _wiki_cache = None
    _wiki_retry_after = 0.0


def catalog_index(session) -> dict[str, str]:
    """``task_key`` -> catalogued task name, from the plugin manifest.

    Returns ``{}`` when the registry row is missing or unreadable, which is the
    "generator has not run" case and must read as "cannot confirm", never as
    "no such task".
    """
    global _catalog_cache
    now = time.monotonic()
    if _catalog_cache is not None and _catalog_cache[0] > now:
        return _catalog_cache[1]

    import json

    index: dict[str, str] = {}
    try:
        from db.models import PluginManifestSection

        row = (
            session.query(PluginManifestSection)
            .filter(PluginManifestSection.key == MANIFEST_SECTION)
            .first()
        )
        if row is not None:
            payload = json.loads(row.payload)
            tasks = payload.get("tasks") if isinstance(payload, dict) else None
            for task in tasks or []:
                name = (task or {}).get("name")
                if name:
                    index.setdefault(task_key(name), str(name))
    except Exception as e:
        print(f"[CATasks] catalog read failed: {e}")
        return {}

    # Never cache an empty index: the row is written by a script that may run
    # long after this process booted, and caching the miss would pin every
    # submission to "unverified" until a restart (the same trap documented in
    # web_api/routes/player_state.py).
    if index:
        _catalog_cache = (now + _CATALOG_TTL_SECONDS, index)
    return index


def _cached_wiki_index(cache) -> dict[str, str] | None:
    """The wiki name index from Redis, or None if absent/unusable."""
    if cache is None:
        return None
    try:
        raw = cache.get(WIKI_CACHE_KEY)
    except Exception as e:
        print(f"[CATasks] wiki cache read failed: {e}")
        return None
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        import json

        names = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(names, list) or not names:
        return None
    return {task_key(n): str(n) for n in names if n}


async def _fetch_wiki_index(cache) -> dict[str, str]:
    """Fetch the full task list from the wiki and refresh both caches."""
    try:
        import asyncio

        import osrs_api

        client = osrs_api.create_client(cache=cache)
        try:
            # Bounded explicitly: aiohttp's default is a 300s total per
            # request, and this runs inside the webhook consumer, so a wiki
            # that accepts the connection and then stalls would park a worker
            # (and its in-flight submission) for minutes per page. Giving up is
            # cheap — the name goes down as unverified and the next window
            # retries.
            names = await asyncio.wait_for(
                client.semantic.get_combat_achievement_names(),
                timeout=WIKI_FETCH_TIMEOUT_SECONDS,
            )
        finally:
            await client.close()
    except Exception as e:
        print(f"[CATasks] wiki fetch failed: {e}")
        return {}

    if not names:
        return {}
    if cache is not None:
        try:
            import json

            cache.set(
                WIKI_CACHE_KEY,
                json.dumps(sorted(names)),
                ex=WIKI_CACHE_TTL_SECONDS,
            )
        except Exception as e:
            print(f"[CATasks] wiki cache write failed: {e}")
    return {task_key(n): str(n) for n in names if n}


async def wiki_index(cache=None, refresh: bool = False) -> dict[str, str]:
    """``task_key`` -> wiki task name.

    Served from the process-local copy, then Redis, then api.php. ``refresh``
    skips the two caches, for a caller that has already looked and not found
    what it needs.

    A live fetch happens at most once per :data:`WIKI_REFETCH_MIN_SECONDS`
    **per process**, not globally — ``ca_processor`` runs in the queue
    consumer, in the six intake API workers and in the core bot. The bound is
    therefore N processes per window, which is what the Redis copy is for: only
    the first process to look pays for a fetch, and the rest read its result.

    Returns ``{}`` when nothing is available, which reads as "cannot confirm".
    """
    global _wiki_cache, _wiki_retry_after
    now = time.monotonic()
    cached = _wiki_cache[1] if (_wiki_cache is not None and _wiki_cache[0] > now) else None

    if not refresh:
        if cached is not None:
            return cached
        from_redis = _cached_wiki_index(cache)
        if from_redis:
            _wiki_cache = (now + WIKI_CACHE_TTL_SECONDS, from_redis)
            return from_redis

    if now < _wiki_retry_after:
        return cached or {}
    # Stamped before the await, not after: a fetch that takes 30s must not let
    # every submission arriving meanwhile start one of its own.
    _wiki_retry_after = now + WIKI_REFETCH_MIN_SECONDS

    fetched = await _fetch_wiki_index(cache)
    if fetched:
        _wiki_cache = (time.monotonic() + WIKI_CACHE_TTL_SECONDS, fetched)
        return fetched
    return cached or {}


async def resolve_task_name(raw, session=None, cache=None) -> ResolvedTask:
    """Clean, then confirm, one submitted task name.

    ``session`` and ``cache`` are optional: without them the markup strip still
    happens and the result is simply ``unverified``. Nothing here raises — the
    submission must survive a broken catalog, a broken Redis and a blocklisted
    wiki alike.
    """
    cleaned = clean_task_name(raw)
    raw_text = "" if raw is None else str(raw)
    if not cleaned:
        return ResolvedTask(name=cleaned, source="unverified", raw=raw_text)

    key = task_key(cleaned)
    if session is not None:
        catalogued = catalog_index(session).get(key)
        if catalogued:
            return ResolvedTask(name=catalogued, source="catalog", raw=raw_text)

    # Catalog miss. Either the registry predates this task or the name is junk;
    # only the wiki can tell those apart, and it holds the whole list, so it is
    # consulted per-list rather than per-name.
    try:
        found = (await wiki_index(cache)).get(key)
        if not found:
            # Absent from a cached list too — which is also what a task
            # released since that list was built looks like. Ask for a live
            # one; wiki_index throttles that, so a junk name cannot turn into
            # a fetch per submission.
            found = (await wiki_index(cache, refresh=True)).get(key)
    except Exception as e:
        print(f"[CATasks] wiki lookup failed for {cleaned!r}: {e}")
        found = None
    if found:
        return ResolvedTask(name=found, source="wiki", raw=raw_text)

    return ResolvedTask(name=cleaned, source="unverified", raw=raw_text)


#: Key holding the unverified-name counter, and the caps that keep it bounded.
#: The members are submitted strings, so "bounded by the number of distinct
#: task names" only holds for names that came from the game. A caller that can
#: choose them can choose an unlimited number of long ones, so the length is
#: truncated and the set is trimmed to its most frequent members once it grows
#: past the cap — the tail is single-hit noise, which is exactly what this is
#: not for. A real new task appears thousands of times a day and survives.
UNVERIFIED_KEY = "ca:unverified_tasks:v1"
_UNVERIFIED_MAX_NAME_CHARS = 64
_UNVERIFIED_MAX_MEMBERS = 500


def note_unverified(cache, task_name: str) -> None:
    """Record an unrecognized task name for review.

    A sorted set rather than a log line alone: the interesting question is
    "which unknown names are arriving repeatedly" (a new task the registry needs
    rebuilding for) versus "which arrived once" (a typo or a spoof), and a
    counter answers it directly. Best-effort — never fails a submission.
    """
    if cache is None or not task_name:
        return
    task_name = task_name[:_UNVERIFIED_MAX_NAME_CHARS]
    try:
        size = cache.zincrby(UNVERIFIED_KEY, 1, task_name)
        if size is not None and cache.zcard(UNVERIFIED_KEY) > _UNVERIFIED_MAX_MEMBERS:
            # Drop the least-frequent members. Rank 0 is the lowest score, so
            # this removes the one-hit tail and keeps whatever is arriving in
            # volume, which is the only thing worth acting on.
            cache.zremrangebyrank(
                UNVERIFIED_KEY, 0, -(_UNVERIFIED_MAX_MEMBERS + 1)
            )
    except Exception as e:
        print(f"[CATasks] unverified-task record failed: {e}")
