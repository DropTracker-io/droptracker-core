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

#: Kinds the clan_broadcast processor records. The rest are classify-only:
#: recognized (so they don't pollute the unknown metric) but intentionally
#: not tracked from chat — plugin submissions carry strictly richer data for
#: every one of them. personal_best joined in v2 (2026-08-05): the broadcast
#: line carries activity, time and — for raids — an explicit team size,
#: which is enough for the PB boards when the write gate never overwrites a
#: faster stored time.
TRACKED_KINDS = frozenset(
    {"item_drop", "raid_drop", "clue_item", "pet", "collection_log", "personal_best"}
)

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")

#: Machine-readable metadata Jagex prefixes to some broadcasts, pipe-terminated:
#: ``CA_ID:112|hype mann has completed a master combat task: ...``. It is aimed
#: at clients that key off the id, so it must reach neither a Discord reader nor
#: a pattern — an unstripped marker occupies ^ and silently kills the whole kind.
#: Deliberately generic (repeated markers and future keys fold too) but tight
#: enough that no broadcast prose or player name can match it: SCREAMING_KEY,
#: digits, pipe. The id is dropped; every kind we parse is identified by text.
_METADATA_PREFIX_RE = re.compile(r"^(?:[A-Z][A-Z0-9_]*:\d+\|)+")


def clean_broadcast_text(raw) -> str:
    """Client markup + whitespace normalization for one broadcast line.

    Strips ``<img=..>``/``<col=..>`` spans and leading Jagex metadata markers
    (see :data:`_METADATA_PREFIX_RE`), folds non-breaking spaces (Jagex renders
    them inside player names) to plain spaces, and collapses runs of whitespace.
    Returns ``""`` for anything non-string-like.

    This is the single choke point for BOTH consumers — the bridge mirror stages
    the cleaned text and every pattern matches against it — so whatever is
    stripped here is gone from Discord and from parsing alike.
    """
    if raw is None:
        return ""
    text = _TAG_RE.sub("", str(raw))
    text = text.replace(" ", " ")
    text = _WS_RE.sub(" ", text).strip()
    # After the tag/whitespace pass: a marker can sit behind a colour span, and
    # the pattern anchors on ^.
    return _METADATA_PREFIX_RE.sub("", text).strip()


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

# Drops name their source after the coin figure ("... received a drop: Hydra's
# claw (48,810,952 coins) from Alchemical Hydra."). Both clauses are optional
# and independently so — untradeables print no value, and some wordings print
# no source — but the suffix is NOT optional to handle: without it the lazy
# item group swallows "(48,810,952 coins) from Alchemical Hydra" whole, the
# item fails to resolve and the whole broadcast is dropped on the floor. The
# source is captured so the drop can attach to the real npc_list row.
_ITEM_DROP_RE = re.compile(
    r"^(?P<player>.+?) received a drop: "
    r"(?:(?P<quantity>[\d,]+) x )?"
    r"(?P<item>.+?)"
    r"(?: \((?P<value>[\d,]+) coins\))?"
    r"(?: from (?P<source>.+?))?\.?$"
)

_RAID_DROP_RE = re.compile(
    r"^(?P<player>.+?) received special loot from a raid: (?P<item>.+?)\.?$"
)

# Same optional source clause as drops: unverified for clue items (no such
# line has been seen in the wild yet), carried defensively because the failure
# mode if Jagex prints one is silent loss of the whole broadcast.
_CLUE_ITEM_RE = re.compile(
    r"^(?P<player>.+?) received a clue item: "
    r"(?P<item>.+?)"
    r"(?: \((?P<value>[\d,]+) coins\))?"
    r"(?: from (?P<source>.+?))?\.?$"
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


def _source_extra(m) -> dict:
    """``{"source": name}`` when the line named where the loot came from."""
    try:
        source = (m["source"] or "").strip()
    except IndexError:
        return {}
    return {"source": source} if source else {}


def _item_drop(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="item_drop",
        player=m["player"],
        item_name=m["item"],
        quantity=_int(m["quantity"]),
        value_gp=_gp(m["value"]),
        extra=_source_extra(m),
    )


def _raid_drop(m) -> ParsedBroadcast:
    return ParsedBroadcast(kind="raid_drop", player=m["player"], item_name=m["item"])


def _clue_item(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="clue_item",
        player=m["player"],
        item_name=m["item"],
        value_gp=_gp(m["value"]),
        extra=_source_extra(m),
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
#: "... has completed a master combat task: Perfect Crystalline Hunllef." The
#: tier is matched loosely rather than enumerated (easy…grandmaster, with "an"
#: before elite): the literal "combat task:" already makes this unambiguous, and
#: a tier we don't know about must still classify. Arrives behind a CA_ID marker
#: that clean_broadcast_text has already removed.
_CA_RE = re.compile(
    r"^(?P<player>.+?) has completed (?:an? )?(?P<tier>\w+) combat task: (?P<task>.+?)\.?$"
)
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
#: Raid PB activities embed the bracket: "Chambers of Xeric (Team Size: 5)".
#: Solo-boss broadcasts carry no size (the processor brackets those "Solo").
_PB_TEAM_SIZE_RE = re.compile(r"\s*\(Team [Ss]ize:\s*(?P<size>[^)]+)\)\s*")
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
#: Clan-channel presence churn — a member logging in or out of the channel, NOT
#: joining or leaving the clan (_INVITE_RE / _LEFT_RE cover membership). By far
#: the highest-volume broadcast there is: 95 of 101 lines on a sample day, which
#: is why they used to dominate the unknown-shape bucket. The optional
#: "the [clan] channel" tail is defensive; the wild wording stops at the verb.
_PRESENCE_RE = re.compile(
    r"^(?P<player>.+?) has (?P<direction>joined|left)"
    r"(?: the (?:clan )?channel)?\.?$"
)
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


def _pb(m) -> ParsedBroadcast:
    """Tracked since v2: activity (bracket stripped), raw team size and the
    display time travel in ``extra``; the processor owns resolution/parsing —
    this module stays pure text."""
    activity = m["activity"]
    team_size = None
    size_match = _PB_TEAM_SIZE_RE.search(activity)
    if size_match:
        team_size = size_match.group("size").strip()
        activity = _PB_TEAM_SIZE_RE.sub(" ", activity).strip()
    return ParsedBroadcast(
        kind="personal_best",
        player=m["player"],
        # The time class is greedy over '.', so it swallows the sentence's
        # trailing period ("... personal best: 1:04.") — strip it here.
        extra={
            "activity": activity,
            "team_size": team_size,
            "time_text": m["time"].rstrip("."),
        },
    )


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


def _combat_achievement(m) -> ParsedBroadcast:
    """Classify-only: the plugin's own CA submissions carry the tier, task and a
    screenshot. Tier and task travel in ``extra`` so tracking them from chat
    later needs no parser change."""
    return ParsedBroadcast(
        kind="combat_achievement",
        player=m["player"],
        extra={"tier": m["tier"], "task": m["task"]},
    )


def _presence(m) -> ParsedBroadcast:
    return ParsedBroadcast(
        kind="presence", player=m["player"], extra={"direction": m["direction"]}
    )


# Order matters only where prefixes overlap: the more specific "clue item" /
# "collection log" / "special loot" phrasings never collide with the generic
# "received a drop", so this is documentation more than necessity. PK-loss is
# last of the "has been" family so invite/expelled win first, and presence sits
# after _LEFT_RE so "has left the clan." stays a membership departure.
_MATCHERS = (
    (_ITEM_DROP_RE, _item_drop),
    (_RAID_DROP_RE, _raid_drop),
    (_CLUE_ITEM_RE, _clue_item),
    (_CLOG_RE, _clog),
    (_PET_RE, _pet),
    (_QUEST_RE, _classified("quest")),
    (_CA_RE, _combat_achievement),
    (_DIARY_RE, _classified("diary")),
    (_XP_RE, _classified("xp_milestone")),
    (_LEVEL_RE, _classified("level_up")),
    (_PB_RE, _pb),
    (_PK_WIN_RE, _pk_win),
    (_INVITE_RE, _classified("invite")),
    (_EXPELLED_RE, _classified("expelled", player_group="player")),
    (_LEFT_RE, _classified("left_clan")),
    (_PRESENCE_RE, _presence),
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
