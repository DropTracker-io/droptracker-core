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

import json
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


# Mirrors data/submissions/drop.py::MAX_SPLIT_SIZE and the website's
# MAX_SPLIT_SIZE in web_api/routes/submissions.py.
MAX_SPLIT_SIZE = 100
MAX_RSN_LENGTH = 12  # OSRS display names cap at 12 characters


def _rsn_key(name: str) -> str:
    """Fold an RSN to the key two spellings of one account share.

    OSRS renders "_" and "-" as spaces and WOM stores the folded form, so
    "X-tra", "X_tra" and "x tra" are all the same player. Kept inline rather
    than importing utils.format's normalize_player_display_equivalence so this
    module stays free of the Discord/DB/PIL imports that module drags in.
    """
    return " ".join(str(name or "").replace("_", " ").replace("-", " ").split()).lower()


def parse_split_players(raw: str | None, receiver_name: str) -> list[str]:
    """Parse the ``/submit drop`` ``split_with`` option into RSNs.

    Discord options can't be lists, so the others in the split arrive as one
    comma-separated string. The receiver is dropped if they list themselves —
    the pipeline counts the receiver separately, so leaving them in would
    double-count them and shrink everyone's share. Raises ValueError with a
    player-facing message on anything unusable.

    Mirrors ``_parse_split`` in web_api/routes/submissions.py so a split reads
    the same whether it came from Discord or the website.
    """
    if not raw or not raw.strip():
        return []
    receiver_key = _rsn_key(receiver_name)
    others: list[str] = []
    seen: set[str] = set()
    for part in raw.replace("\n", ",").split(","):
        name = part.strip()
        if not name:
            continue
        if len(name) > MAX_RSN_LENGTH:
            raise ValueError(f"“{name}” isn't a valid RuneScape name (max 12 characters).")
        key = _rsn_key(name)
        if key == receiver_key or key in seen:
            continue
        seen.add(key)
        others.append(name)
    if len(others) + 1 > MAX_SPLIT_SIZE:
        raise ValueError(f"A split can involve at most {MAX_SPLIT_SIZE} players.")
    return others


def resolve_split_size(split_size: int | None, others: list[str]) -> int | None:
    """Total people in the split, receiver included — or None for no split.

    With no explicit size, the party is exactly the people named. An explicit
    size is what makes an *untracked* or unnamed member countable: it must be at
    least (receiver + everyone named), since a smaller number contradicts the
    names given in the same command.
    """
    named = len(others)
    if split_size is None:
        return named + 1 if named else None
    size = int(split_size)
    if size < 2 or size > MAX_SPLIT_SIZE:
        raise ValueError(f"A split has to be between 2 and {MAX_SPLIT_SIZE} players.")
    if size < named + 1:
        raise ValueError(
            f"You listed {named} other player(s), so the split is at least "
            f"{named + 1} ways — but you entered {size}."
        )
    return size


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
    split_with: str | None = None,
    split_size: int | None = None,
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
        # Split GP tracking: who else was in on it, and how many ways it went.
        # The two are independent — a size larger than the names is exactly how
        # a share taken by someone untracked gets counted.
        others = parse_split_players(split_with, player_name)
        resolved_size = resolve_split_size(split_size, others)
        if others:
            payload["players_included"] = others
        if resolved_size:
            payload["split_size"] = resolved_size
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
    and "true"/"false" back to bools, so plain str() round-trips every scalar.
    Lists must go as JSON, not str(): `str(["a"])` yields `"['a']"`, whose single
    quotes json.loads rejects, and the intake's participant parser would then
    fall through to its comma-split branch and read the brackets and quotes as
    part of the first player's name.
    """
    def encode(v):
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, (list, tuple)):
            return json.dumps(list(v))
        return str(v)

    return {k: encode(v) for k, v in payload.items() if v is not None}


def summarize_submission(sub_type: str, payload: dict) -> str:
    """One-line human summary of what was submitted, for the confirmation
    embed (e.g. "3x Dragon claws from Chambers of Xeric")."""
    if sub_type == "drop":
        qty = int(payload.get("quantity") or 1)
        prefix = f"{qty}x " if qty > 1 else ""
        base = f"{prefix}**{payload['item_name']}** from **{payload['npc_name']}**"
        size = payload.get("split_size")
        if size and int(size) > 1:
            named = payload.get("players_included") or []
            # Spell out any share that went to someone we can't name, so the
            # submitter can see their cut was divided by the whole party.
            unnamed = int(size) - len(named) - 1
            with_who = ", ".join(f"**{n}**" for n in named) if named else ""
            extra = (f"{' + ' if with_who else ''}{unnamed} unnamed" if unnamed > 0 else "")
            who = f" with {with_who}{extra}" if (with_who or extra) else ""
            base += f" — split **{size} ways**{who}"
        return base
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
