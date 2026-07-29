"""Pure helpers for the Discord ``/submit`` manual-submission commands.

The Discord extension (``commands/submissions.py``) forwards to the intake
API's ``/manual-submit`` endpoint — the same pipeline the website's submit
form uses (``web_api/routes/submissions.py``) — so every backend validation
(item/NPC resolution, GE valuation, dedupe, high-value verification, the
per-group manual policies) applies identically to both surfaces.

This module holds the logic that doesn't need Discord or a DB session so it
stays unit-testable (the test conftest stubs ``interactions`` as a MagicMock,
which makes Extension classes impossible to import in tests).
"""
from __future__ import annotations

import re

# Discord-facing type key -> intake /manual-submit submission_type.
# Mirrors _TYPE_MAP in web_api/routes/submissions.py.
SUBMISSION_TYPES = {
    "drop": "drop",
    "clog": "collection_log",
    "pb": "personal_best",
    "ca": "combat_achievement",
    "pet": "pet",
}

CA_TIERS = ("easy", "medium", "hard", "elite", "master", "grandmaster")

# Per-policy phrasing for the pre-submit heads-up, matching the website's
# _POLICY_NOTICE (web_api/routes/submissions.py) so users read the same
# warning on both surfaces.
POLICY_NOTICE = {
    "block": "won't be counted (manual submissions are disabled)",
    "authorized_only": "won't be counted (only authorized members' manual submissions count)",
    "confirm": "will be held for a group admin to approve",
}
POLICY_NOTICE_FALLBACK = "will be reviewed by the group"

_KILL_TIME_RE = re.compile(r"^\d+(:\d{1,2}){0,2}(\.\d{1,3})?$")


def parse_kill_time_ms(raw: str | None) -> int | None:
    """Parse a human kill-time string to milliseconds.

    Accepts "1:23.40", "0:45", "1:02:03.6" and plain seconds ("83.4").
    Returns None when the string doesn't parse or is non-positive.
    Mirrors parseKillTimeMs in the web repo's components/submit-form.tsx.
    """
    s = (raw or "").strip()
    if not s or not _KILL_TIME_RE.match(s):
        return None
    main, _, frac = s.partition(".")
    seconds = 0
    for part in main.split(":"):
        seconds = seconds * 60 + int(part)
    ms = seconds * 1000 + (round(float(f"0.{frac}") * 1000) if frac else 0)
    return ms if ms > 0 else None


def format_ms(ms: int) -> str:
    """Milliseconds -> "m:ss[.t]" / "h:mm:ss[.t]" (web repo's formatMs)."""
    total = ms // 1000
    h, m, sec = total // 3600, (total % 3600) // 60, total % 60
    tenths = round((ms % 1000) / 100)
    sec_str = f"{sec:02d}" + (f".{tenths}" if tenths else "")
    return f"{h}:{m:02d}:{sec_str}" if h else f"{m}:{sec_str}"


def build_manual_payload(
    sub_type: str,
    player_name: str,
    *,
    item_name: str | None = None,
    npc_name: str | None = None,
    quantity: int | None = None,
    value: int | None = None,
    time_ms: int | None = None,
    team_size: int | None = None,
    task: str | None = None,
    tier: str | None = None,
    kc: int | None = None,
) -> dict:
    """Build the intake ``/manual-submit`` payload for one submission.

    Field mapping mirrors web_api/routes/submissions.py exactly (clog/pet use
    ``source`` for the NPC, pb uses ``boss_name``, pet keys off ``pet_name``).
    Raises ValueError on a missing per-type required field — the slash-command
    options already enforce these, so a raise here means a wiring bug.
    """
    if sub_type not in SUBMISSION_TYPES:
        raise ValueError(f"Unknown submission type: {sub_type}")
    payload: dict = {
        "submission_type": SUBMISSION_TYPES[sub_type],
        "player_name": player_name,
        "world_type": "main",
    }
    if sub_type == "drop":
        if not item_name or not npc_name:
            raise ValueError("A drop needs both an item and the NPC it came from.")
        payload["item_name"] = item_name
        payload["npc_name"] = npc_name
        payload["quantity"] = max(1, int(quantity or 1))
        if value is not None and value >= 0:
            # 0/omitted = "unknown": the pipeline recovers the GE price.
            payload["value"] = int(value)
    elif sub_type == "clog":
        if not item_name:
            raise ValueError("A collection log entry needs the item.")
        payload["item_name"] = item_name
        if npc_name:
            payload["source"] = npc_name
        if kc is not None and kc >= 0:
            payload["kc"] = int(kc)
    elif sub_type == "pb":
        if not npc_name:
            raise ValueError("A personal best needs the boss.")
        if not time_ms or time_ms <= 0:
            raise ValueError("A personal best needs the kill time.")
        payload["boss_name"] = npc_name
        payload["time_ms"] = int(time_ms)
        if team_size is not None and team_size >= 1:
            payload["team_size"] = int(team_size)
    elif sub_type == "ca":
        if not task or not tier:
            raise ValueError("A combat achievement needs the task and its tier.")
        payload["task"] = task.strip()
        payload["tier"] = tier.strip().lower()
    elif sub_type == "pet":
        if not item_name:
            raise ValueError("A pet submission needs the pet.")
        payload["pet_name"] = item_name
        if npc_name:
            payload["source"] = npc_name
        if kc is not None and kc >= 0:
            payload["killcount"] = int(kc)
    return payload


def payload_to_form(payload: dict) -> dict:
    """Stringify a payload for multipart forwarding (with an image file).

    The intake endpoint's multipart parser converts digit strings back to ints
    and "true"/"false" back to bools, so plain str() round-trips every value.
    """
    return {k: ("true" if v is True else "false" if v is False else str(v))
            for k, v in payload.items() if v is not None}


def summarize_submission(sub_type: str, payload: dict) -> str:
    """One-line human summary of what was submitted, for the confirmation
    embed (e.g. "3x Dragon claws from Chambers of Xeric")."""
    if sub_type == "drop":
        qty = int(payload.get("quantity") or 1)
        prefix = f"{qty}x " if qty > 1 else ""
        return f"{prefix}**{payload['item_name']}** from **{payload['npc_name']}**"
    if sub_type == "clog":
        src = payload.get("source")
        base = f"**{payload['item_name']}** (collection log)"
        return f"{base} from **{src}**" if src else base
    if sub_type == "pb":
        team = payload.get("team_size")
        suffix = f" (team of {team})" if team and int(team) > 1 else ""
        return f"**{format_ms(int(payload['time_ms']))}** at **{payload['boss_name']}**{suffix}"
    if sub_type == "ca":
        return f"**{payload['task']}** ({str(payload['tier']).capitalize()} combat achievement)"
    if sub_type == "pet":
        src = payload.get("source")
        base = f"**{payload['pet_name']}** (pet)"
        return f"{base} from **{src}**" if src else base
    return "your submission"
