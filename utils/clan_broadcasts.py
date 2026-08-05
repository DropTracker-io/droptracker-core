"""Parsing of in-game clan-chat broadcast lines relayed by the plugin.

The plugin's clan-relay feature forwards ``CLAN_MESSAGE`` system broadcasts
("Player received a drop: ...") verbatim; everything here runs server-side so
pattern fixes never wait on a plugin-hub review cycle. The regexes are
re-derived from Jagex's actual broadcast formats (do NOT copy third-party
parser code — see the TrackScape licensing note in the dev tracker, project
#7).

Only ``TRACKED_KINDS`` feed the ``clan_broadcast`` processor; every other
recognized kind exists so the unparsed-broadcast metric measures genuinely
*unknown* lines (a Jagex rewording we need to react to) rather than known
kinds we deliberately don't track yet.

Broadcast text reaches us with client markup embedded — ``<img=41>`` rank and
ironman icons, ``<col=...>`` color spans — and Jagex renders player names with
non-breaking spaces. :func:`clean_broadcast_text` folds all of that before any
pattern runs; callers matching extracted names against the roster must still
compare via ``utils.format.normalize_player_display_equivalence``, never raw
equality.

This module is intentionally dependency-free (stdlib ``re``/``dataclasses``
only) so it is importable from the intake processors, the bots, and tests
alike.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "ParsedBroadcast",
    "TRACKED_KINDS",
    "clan_slug",
    "clean_broadcast_text",
    "parse_broadcast",
]

#: Kinds the clan_broadcast processor records in v1. The rest are
#: classify-only: recognized (so they don't pollute the unknown metric) but
#: intentionally not tracked from chat — plugin submissions carry strictly
#: richer data for every one of them.
TRACKED_KINDS = frozenset(
    {"item_drop", "raid_drop", "clue_item", "pet", "collection_log"}
)

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def clean_broadcast_text(raw) -> str:
    """Client markup + whitespace normalization for one broadcast line.

    Strips ``<img=..>``/``<col=..>`` spans, folds non-breaking spaces (Jagex
    renders them inside player names) to plain spaces, and collapses runs of
    whitespace. Returns ``""`` for anything non-string-like.
    """
    if raw is None:
        return ""
    text = _TAG_RE.sub("", str(raw))
    text = text.replace(" ", " ")
    return _WS_RE.sub(" ", text).strip()


def clan_slug(raw_clan_name) -> str:
    """Canonical comparison key for an in-game clan name.

    The single identity rule shared by broadcast group-binding
    (``data/submissions/clan_broadcast.py``), the chat bridge
    (``services/clan_chat_bridge.py``) and presence stamping — a
    ``clan_chat_name`` config value and a client-reported clan name must meet
    in exactly one normal form.
    """
    from utils.format import normalize_player_display_equivalence

    return normalize_player_display_equivalence(clean_broadcast_text(raw_clan_name))


@dataclass(frozen=True)
class ParsedBroadcast:
    """One recognized broadcast line.

    ``value_gp`` is the coin figure Jagex printed in the message (None when the
    message carries none — untradeables, raid loot). It is a display hint, not
    an authoritative price: the processor prices items itself and only uses
    this as a divergence sanity check.
    """

    kind: str
    player: str | None = None
    item_name: str | None = None
    quantity: int = 1
    value_gp: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def tracked(self) -> bool:
        return self.kind in TRACKED_KINDS


def _gp(raw) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _int(raw, default=1) -> int:
    value = _gp(raw)
    return default if value is None else value


# --- Tracked kinds ---------------------------------------------------------
# Player names are 1-12 chars and cannot contain ':', so a lazy leading group
# terminated by the literal verb phrase cannot over-capture. Trailing periods
# are optional everywhere: Jagex is inconsistent across broadcast kinds.

_ITEM_DROP_RE = re.compile(
    r"^(?P<player>.+?) received a drop: "
    r"(?:(?P<quantity>[\d,]+) x )?"
    r"(?P<item>.+?)"
    r"(?: \((?P<value>[\d,]+) coins\))?\.?$"
)

_RAID_DROP_RE = re.compile(
    r"^(?P<player>.+?) received special loot from a raid: (?P<item>.+?)\.?$"
)

_CLUE_ITEM_RE = re.compile(
    r"^(?P<player>.+?) received a clue item: "
    r"(?P<item>.+?)"
    r"(?: \((?P<value>[\d,]+) coins\))?\.?$"
)

_CLOG_RE = re.compile(
    r"^(?P<player>.+?) received a new collection log item: "
    r"(?P<item>.+?) \((?P<slots>\d+)/(?P<total>\d+)\)\.?$"
)

# Pronouns vary with the character ("he's"/"she's", "his"/"her"), and the
# duplicate-pet variant reads "would have been followed" — wildcard the
# pronoun segment rather than enumerating Jagex's choices (the plugin's own
# PetHandler.CLAN_REGEX does the same). The trailing count+unit ("at 194
# kills", "at 12,000,000 XP") is absent on some skilling pets, so it is
# optional.
_PET_RE = re.compile(
    r"^(?P<player>.+?) "
    r"(?:has a funny feeling like .+? followed|"
    r"feels something weird sneaking into .+? backpack): "
    r"(?P<pet>.+?)"
    r"(?: at (?P<count>[\d,]+) (?P<unit>.+?))?\.?$"
)


def _item_drop(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="item_drop",
        player=m["player"],
        item_name=m["item"],
        quantity=_int(m["quantity"]),
        value_gp=_gp(m["value"]),
    )


def _raid_drop(m) -> ParsedBroadcast:
    return ParsedBroadcast(kind="raid_drop", player=m["player"], item_name=m["item"])


def _clue_item(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="clue_item",
        player=m["player"],
        item_name=m["item"],
        value_gp=_gp(m["value"]),
    )


def _clog(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="collection_log",
        player=m["player"],
        item_name=m["item"],
        extra={"log_slots": int(m["slots"]), "log_total": int(m["total"])},
    )


def _pet(m) -> ParsedBroadcast:
    extra = {}
    if m["count"] is not None:
        extra = {"milestone_count": _int(m["count"]), "milestone_unit": m["unit"]}
    return ParsedBroadcast(
        kind="pet", player=m["player"], item_name=m["pet"], extra=extra
    )


# --- Classify-only kinds ---------------------------------------------------
# Recognition keeps the unknown-broadcast metric honest; extraction is the
# minimum useful (subject player where unambiguous).

_QUEST_RE = re.compile(r"^(?P<player>.+?) has completed a quest: (?P<quest>.+?)\.?$")
_DIARY_RE = re.compile(
    r"^(?P<player>.+?) has completed the (?P<tier>Easy|Medium|Hard|Elite) "
    r"(?P<diary>.+?) diary\.?$"
)
_LEVEL_RE = re.compile(
    r"^(?P<player>.+?) has reached (?:a )?(?:the highest possible )?"
    r"(?P<skill>.+?) level(?: of)? (?P<level>[\d,]+)\.?$"
)
_XP_RE = re.compile(
    r"^(?P<player>.+?) has reached (?P<xp>[\d,]+) XP in (?P<skill>.+?)\.?$"
)
_PB_RE = re.compile(
    r"^(?P<player>.+?) has achieved a new (?P<activity>.+?) personal best: "
    r"(?P<time>[\d:.]+)\.?$"
)
_PK_WIN_RE = re.compile(
    r"^(?P<player>.+?) has defeated (?P<loser>.+?) and received "
    r"\((?P<value>[\d,]+) coins\) worth of loot!?$"
)
_PK_LOSS_RE = re.compile(
    r"^(?P<player>.+?) has been defeated by (?P<winner>.+?)"
    r"(?: in (?:The )?Wilderness)?\.?$"
)
_INVITE_RE = re.compile(
    r"^(?P<player>.+?) has been invited into the clan by (?P<inviter>.+?)\.?$"
)
_LEFT_RE = re.compile(r"^(?P<player>.+?) has left the clan\.?$")
_EXPELLED_RE = re.compile(
    r"^(?P<mod>.+?) has expelled (?P<player>.+?) from the clan\.?$"
)
_COFFER_RE = re.compile(
    r"^(?P<player>.+?) has (?P<direction>deposited|withdrawn) "
    r"(?P<value>[\d,]+) coins (?:into|from) the (?:clan )?coffer\.?$"
)


def _classified(kind: str, player_group: str = "player"):
    def build(m) -> ParsedBroadcast:
        return ParsedBroadcast(kind=kind, player=m[player_group])

    return build


def _pk_win(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="pk",
        player=m["player"],
        value_gp=_gp(m["value"]),
        extra={"defeated": m["loser"], "won": True},
    )


def _pk_loss(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="pk", player=m["player"], extra={"defeated_by": m["winner"], "won": False}
    )


def _coffer(m) -> ParsedBroadcast:
    kind = "coffer_donation" if m["direction"] == "deposited" else "coffer_withdrawal"
    return ParsedBroadcast(kind=kind, player=m["player"], value_gp=_gp(m["value"]))


# Order matters only where prefixes overlap: the more specific "clue item" /
# "collection log" / "special loot" phrasings never collide with the generic
# "received a drop", so this is documentation more than necessity. PK-loss is
# last of the "has been" family so invite/expelled win first.
_MATCHERS = (
    (_ITEM_DROP_RE, _item_drop),
    (_RAID_DROP_RE, _raid_drop),
    (_CLUE_ITEM_RE, _clue_item),
    (_CLOG_RE, _clog),
    (_PET_RE, _pet),
    (_QUEST_RE, _classified("quest")),
    (_DIARY_RE, _classified("diary")),
    (_XP_RE, _classified("xp_milestone")),
    (_LEVEL_RE, _classified("level_up")),
    (_PB_RE, _classified("personal_best")),
    (_PK_WIN_RE, _pk_win),
    (_INVITE_RE, _classified("invite")),
    (_EXPELLED_RE, _classified("expelled", player_group="player")),
    (_LEFT_RE, _classified("left_clan")),
    (_PK_LOSS_RE, _pk_loss),
    (_COFFER_RE, _coffer),
)


def parse_broadcast(raw) -> ParsedBroadcast | None:
    """Parse one relayed broadcast line, or None when the shape is unknown.

    Input may carry client markup — it is cleaned here, so callers can pass
    the relayed text verbatim. A None return is the signal the caller should
    count (and sample-log) as an unknown broadcast shape.
    """
    text = clean_broadcast_text(raw)
    if not text:
        return None
    for pattern, build in _MATCHERS:
        m = pattern.match(text)
        if m:
            return build(m)
    return None
