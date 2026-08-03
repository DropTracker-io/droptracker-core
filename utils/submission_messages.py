"""Player-facing wording for rejected manual submissions.

Both manual-submission surfaces — the website's submit form
(``web_api/routes/submissions.py``) and the Discord ``/submit`` commands
(``commands/submissions.py``) — forward to the intake API's
``/manual-submit``, so both get the same rejection payload back and should
show the player the same sentence. This module is deliberately pure (no db,
services or Discord imports) so the web API can import it on its request
path and so it stays unit-testable.

It solves two separate problems:

1. **Finding the reason at all.** ``/manual-submit`` answers a *processor*
   rejection with HTTP **200** and ``{"success": false, "message": ...}``;
   only transport/parse failures carry an ``"error"`` key. A reader that
   checks ``error`` alone finds nothing and falls back to the status code —
   which is how a player submitting coins was shown the literal text
   "Intake returned 200.". See ``rejection_reason``.

2. **Saying it in English.** The pipeline's messages are written for logs
   ("NPC ID could not be resolved for X, aborting"), so ``friendly_rejection``
   rewrites the ones a manual submitter can actually trigger.
"""
from __future__ import annotations

import re

# Shown when the intake gave us nothing usable to explain itself with.
GENERIC_REJECTION = (
    "We couldn't record that submission. Double-check the details and try again."
)

# Shown when the intake itself is unhealthy or misconfigured — not the
# player's fault, and nothing they can fix by editing the form.
SERVICE_UNAVAILABLE = (
    "Manual submissions are temporarily unavailable. Please try again in a few "
    "minutes."
)

# (pattern, replacement) for every rejection a manual submitter can actually
# reach. Order matters: the exact-wording rules come before the ones that
# capture an interpolated name, so "Item was not found in the database" isn't
# read as an item literally named "was".
_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # data/submissions/drop.py — the >1M wiki drop-source check.
    (
        re.compile(r"^item\s+(?P<item>.+?)\s+is not from npc\s+(?P<npc>.+?)\.?$", re.I),
        "We couldn't verify that {item} is a drop from {npc}, so it wasn't "
        "recorded. Double-check the source you picked — if it is correct, let a "
        "DropTracker admin know so we can look into it.",
    ),
    (
        re.compile(r"^item was not found in the database\.?$", re.I),
        "We don't recognise that item, so it couldn't be recorded. Pick it from "
        "the suggestions if you typed it in by hand.",
    ),
    (
        re.compile(r"^item\s+(?P<item>.+?)\s+not found in the database\.?$", re.I),
        "{item} isn't in our item list yet, so it couldn't be recorded. Pick the "
        "item from the suggestions if you typed it in by hand.",
    ),
    (
        re.compile(r"^npc id could not be resolved for\s+(?P<npc>.+?),?\s*aborting\.?$", re.I),
        "We don't recognise “{npc}” as a boss or NPC. Pick one from the "
        "suggestions if you typed it in by hand.",
    ),
    (
        re.compile(
            r"^(player\s+.+?\s+not found in the database|"
            r"player not found or could not be created)\.?$",
            re.I,
        ),
        "That account isn't registered with DropTracker yet — it needs to submit "
        "once through the RuneLite plugin before you can add drops for it.",
    ),
    (
        re.compile(
            r"^(player\s+.+?\s+failed auth check|player authentication failed)\.?$",
            re.I,
        ),
        "We couldn't verify that account belongs to you. Re-link it from your "
        "settings and try again.",
    ),
    (
        re.compile(r"^missing required player identification fields\.?$", re.I),
        "We couldn't tell which account this submission is for. Reload the page "
        "and try again.",
    ),
    (
        re.compile(r"^drop value exceeds the plausible maximum.*$", re.I),
        "That total value is higher than a single drop can be. Check the quantity "
        "and value you entered.",
    ),
    (
        re.compile(r"^invalid drop quantity\.?$", re.I),
        "Enter a whole number of at least 1 for the quantity.",
    ),
    (
        re.compile(r"^failed to create drop\.?$", re.I),
        "Something went wrong while saving that drop. Try again in a moment.",
    ),
    # webhook.py wraps an unhandled processor exception as
    # "Error processing submission: <repr>" — never show a player a traceback.
    (
        re.compile(r"^error processing submission:", re.I),
        "Something went wrong while processing that submission. Try again in a "
        "moment — if it keeps happening, let a DropTracker admin know.",
    ),
    # The /manual-submit shared-secret gate (401/503). A player can't act on
    # either, so both read as a service problem rather than a rejection.
    (
        re.compile(r"^(unauthorized|manual submissions are not configured.*)\.?$", re.I),
        SERVICE_UNAVAILABLE,
    ),
)


def rejection_reason(data) -> str | None:
    """The raw reason string from a ``/manual-submit`` response body.

    ``error`` is set by the endpoint's own validation and transport failures;
    ``message``/``notice`` carry a processor's verdict, which is what a real
    rejection looks like. Returns ``None`` when the body explains nothing.
    """
    if not isinstance(data, dict):
        return None
    for key in ("error", "message", "notice"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def friendly_rejection(data, fallback: str = GENERIC_REJECTION) -> str:
    """A player-facing sentence for a rejected ``/manual-submit`` response.

    Falls back to the pipeline's own wording when the reason is real but
    unrecognised (better a blunt reason than none), and to ``fallback`` when
    the response explained nothing at all.
    """
    reason = rejection_reason(data)
    if not reason:
        return fallback
    for pattern, replacement in _RULES:
        match = pattern.match(reason)
        if match:
            return replacement.format(**match.groupdict())
    return reason
