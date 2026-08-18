"""Single source of truth for submission-type routing.

Every intake transport routes through this module:

* ``api/routes/webhook.py``      — synchronous API path (non-queue fallback)
* ``workers/webhook_consumer.py``— fast-accept queue consumer (live in prod)
* ``bots/webhook_bot.py``        — legacy Discord-webhook channel reader

Those three used to carry three hand-maintained copies of the same match
statement, and they drifted. The webhook reader's copy never grew a ``death``,
``diary`` or ``quest`` branch, so main-world submissions of those types fell
out of its if/elif chain and were discarded with no row, no log line and no
rejection: every one of the 29,850 ``player_deaths`` rows was ``used_api=1``
because a death could not physically survive that path. Quests were worse —
detected, then dropped by a ``continue`` under a commented-out call.

**Adding or renaming a submission type means editing THIS module and nothing
else.** ``tests/unit/test_submission_dispatch_parity.py`` asserts that no
caller has grown a private branch of its own.

The alias table is the union of every spelling the plugin has ever sent;
older builds are still in the wild, so entries here are removed only when
that build is provably gone.
"""

MAIN_WORLD_TYPE = "main"
SEASONAL_WORLD_TYPE = "seasonal"


# Raw type string (as the plugin sends it) -> canonical type.
# Anything not listed passes through unchanged after lower/strip.
TYPE_ALIASES = {
    "other": "drop",
    "npc": "drop",
    "kill_time": "personal_best",
    "npc_kill": "personal_best",
    "experience_update": "experience",
    "experience_milestone": "experience",
    "level_up": "experience",
    # legacy type string still sent by older plugin builds
    "xp_milestone": "experience",
    "quest_completion": "quest",
    "player_death": "death",
    "achievement_diary": "diary",
    "diary_completion": "diary",
}


# Canonical type -> processor function name exported by ``data.submissions``.
# Membership in this mapping IS the definition of "supported".
_PROCESSORS = {
    "drop": "drop_processor",
    "collection_log": "clog_processor",
    "personal_best": "pb_processor",
    "combat_achievement": "ca_processor",
    "experience": "experience_processor",
    "quest": "quest_processor",
    "death": "death_processor",
    "diary": "diary_processor",
    "pet": "pet_processor",
    "adventure_log": "adventure_log_processor",
    "clan_broadcast": "clan_broadcast_processor",
    "clan_chat": "clan_chat_processor",
}

SUPPORTED_TYPES = frozenset(_PROCESSORS)

# Types with seasonal mirror tables. ``experience`` and ``adventure_log`` are
# absent because their processors take no ``world_type`` argument at all —
# calling them for a seasonal world is a TypeError, not a no-op — and the
# clan relay types are main-world chat with nothing to mirror.
SEASONAL_TYPES = frozenset({
    "drop",
    "collection_log",
    "personal_best",
    "combat_achievement",
    "pet",
    "quest",
    "death",
    "diary",
})


def normalize_world_type(raw_world_type):
    """Canonical world type; anything empty/absent means the main game."""
    if raw_world_type is None:
        return MAIN_WORLD_TYPE
    normalized = str(raw_world_type).strip().lower()
    return normalized or MAIN_WORLD_TYPE


def normalize_submission_type(raw_submission_type):
    """Canonical submission type for any spelling the plugin has ever sent."""
    normalized = str(raw_submission_type or "").strip().lower()
    return TYPE_ALIASES.get(normalized, normalized)


def is_supported(submission_type) -> bool:
    """Whether this type (raw or canonical) has a processor behind it."""
    return normalize_submission_type(submission_type) in SUPPORTED_TYPES


def resolve_submission_type(declared_type, title, field_names, field_values):
    """The submission type a Discord-webhook embed represents, or None.

    The plugin stamps every embed with a ``type`` field (BaseEventHandler
    #createEmbed), so that is read first — which is what lets the webhook
    reader pick up any type added to this module later, with no branch of
    its own.

    The value-sniffing fallback is for embeds predating the ``type`` field.
    It is deliberately NOT the primary path: it *was* the primary path in
    bots/webhook_bot.py, and because it only recognized types someone
    remembered to write a branch for, main-world deaths and diaries were
    never detected at all and quests were detected and then discarded.
    """
    declared = normalize_submission_type(declared_type)
    if declared in SUPPORTED_TYPES:
        return declared

    field_names = field_names or ()
    field_values = field_values or ()

    if "collection_log" in field_values:
        return "collection_log"
    if "combat_achievement" in field_values:
        return "combat_achievement"
    if "npc_kill" in field_values or "kill_time" in field_values:
        return "personal_best"
    if (title and "received some drops" in title) or "drop" in field_values:
        return "drop"
    if any(v in field_values for v in
           ("experience_update", "experience_milestone", "level_up", "xp_milestone")):
        return "experience"
    if "quest_completion" in field_values:
        return "quest"
    if "player_death" in field_values or "death" in field_values:
        return "death"
    if "achievement_diary" in field_values or "diary_completion" in field_values:
        return "diary"
    if "pet" in field_values and "pet_name" in field_names:
        return "pet"
    if "adventure_log" in field_values:
        return "adventure_log"
    return None


async def dispatch_submission(submission_type, data, session, *, world_type=MAIN_WORLD_TYPE):
    """Route one submission to its processor and return the SubmissionResponse.

    ``submission_type`` may be any alias; it is normalized here so callers
    cannot skew by normalizing differently. Returns ``None`` when the type has
    no processor for this world — callers report that in their own idiom
    (the API answers 200 with a rejection, the reader logs a warning), since
    only they know how to reach the client.

    Deliberately does NOT commit: the caller owns the session and its
    transaction boundary.
    """
    from data import submissions

    norm_type = normalize_submission_type(submission_type)
    world = normalize_world_type(world_type)

    if world == SEASONAL_WORLD_TYPE:
        if norm_type not in SEASONAL_TYPES:
            return None
        processor = getattr(submissions, _PROCESSORS[norm_type])
        return await processor(data, external_session=session, world_type=SEASONAL_WORLD_TYPE)

    processor_name = _PROCESSORS.get(norm_type)
    if processor_name is None:
        return None
    processor = getattr(submissions, processor_name)
    # Main-world calls omit world_type so the two processors that do not accept
    # it (experience, adventure_log) route through the same single call site.
    return await processor(data, external_session=session)
