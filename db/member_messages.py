"""Members' own notification messages — the rules every surface shares.

A member writes the line their clan sees when they die ("{player_name} forgot
to pray against {killer}"), log a collection log slot, get a drop or set a
personal best. Each is a *message type* (:data:`MESSAGE_KINDS`): its own
placeholders and its own group switches, with every rule below shared. Four
places read or write those messages, and all of them import from here so they
cannot disagree about what a valid message is or which line gets posted:

  * ``web_api/routes/member_messages.py`` — the website editor for members and
    the moderation card for group leaders;
  * ``api/routes/member_messages.py`` — the RuneLite plugin's editor;
  * ``services/player_settings_panel.py`` — ``/settings`` in Discord;
  * ``services/notification_service.py`` — the senders, which pick the line.

**Groups opt in, per type.** Nothing a member writes reaches a channel unless
the group set that type's switch (``allow_member_death_messages``,
``allow_member_clog_messages``, ...), and a leader can still block individual
members (``group_member_message_blocks``, which covers every type). It is off by default because turning
it on gives every member a line in the clan's channel, and that is the group's
decision to make, not the member's.

**Which line wins**, per group and per notification: the member's own message
when the group allows it and has not blocked them, else the group's randomized
messages (``<type>_message_variants``), else the sender's own line. A message whose placeholders come out blank for
this death is skipped rather than posted half-empty ("was killed by ."), so a
member whose every message names the killer falls through to the next source
when the killer is unknown.

**Validated on write, sanitized again on send.** A write is refused, with a
reason the editor can show, for mentions, links, custom emoji, headings, unknown
placeholders or line breaks. The sender strips the same things again, from the
template and from the client-sent values substituted into it, because a stored
row can predate a rule and a payload carries whatever the client put in it.

Pure apart from the helpers at the bottom, which take a session and import
their models lazily — tests load this file by path under the stubbed ``db``
package (see tests/conftest.py).
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass

#: ``player_custom_messages.message_type`` values.
MESSAGE_TYPE_DEATH = "death"
MESSAGE_TYPE_CLOG = "clog"
MESSAGE_TYPE_DROP = "drop"
MESSAGE_TYPE_PB = "pb"

#: Group config key. Truthy means members' own death messages are posted.
ALLOW_DEATH_CONFIG_KEY = "allow_member_death_messages"

#: Limits, mirrored by the website (packages/api-types) and the plugin
#: (DeathMessageRules.java). Five is also the most fields a Discord modal holds.
MAX_MESSAGES = 5
MAX_MESSAGE_LENGTH = 150

#: ``player_custom_messages.updated_via``.
VIA_WEB = "web"
VIA_DISCORD = "discord"
VIA_PLUGIN = "plugin"
VIA_STAFF = "staff"

#: Placeholders a member's death message may use, in display order. The
#: samples are what the editors show in their previews.
DEATH_TOKENS: tuple[dict, ...] = (
    {"token": "{player_name}", "help": "Your name", "sample": "Zezima"},
    {"token": "{killer}", "help": "What killed you", "sample": "Vorkath"},
    {"token": "{location}", "help": "Where you died", "sample": "Ungael"},
    {"token": "{value_lost}", "help": "GP value of the items you lost", "sample": "4.2M"},
    {"token": "{value_kept}", "help": "GP value of the items you kept", "sample": "18.9M"},
    {"token": "{killer_combat_level}", "help": "Your killer's combat level", "sample": "392"},
)

#: Also accepted, so a line copied from a group's own death messages still works.
DEATH_TOKEN_ALIASES = {"{source}": "{killer}", "{region_name}": "{location}"}

MEMBER_DEATH_TOKENS = frozenset(
    [doc["token"] for doc in DEATH_TOKENS] + list(DEATH_TOKEN_ALIASES)
)

#: Placeholders that describe the death itself. A template using one that is
#: blank for this death is skipped. Media and bookkeeping tokens ({video_link},
#: {timestamp}, ...) may be blank: a line reads fine without them.
DEATH_DATA_TOKENS = frozenset({
    "{killer}", "{source}", "{location}", "{region_name}", "{region_id}",
    "{value_lost}", "{value_kept}", "{killer_combat_level}",
})

#: Values that came from the client and are cleaned before they go into a line.
#: ``{player_name}`` is absent on purpose: the sender builds it (a profile link,
#: or the formatted name, which carries the player's own opted-in ping).
_CLIENT_VALUE_TOKENS = frozenset({
    "{killer}", "{source}", "{location}", "{region_name}", "{region_id}",
    "{killer_combat_level}", "{player_name_plain}",
})

_CLIENT_VALUE_MAX_LENGTH = 100


@dataclass(frozen=True)
class MessageKind:
    """One type of member message: what it may say and where groups switch it on.

    ``tokens`` are documented to members (with editor samples); ``aliases`` are
    also accepted so a line copied from the group's own messages still works.
    ``data_tokens`` describe the event itself: a template using one that is
    blank for this notification is skipped. ``client_tokens`` carry
    client-sent text and are cleaned before they go into a line. The group's
    config keys are ``allow_key`` (members' messages on), ``variants_key``
    (the group's own randomized list) and ``as_embed_key`` (line replaces the
    embed description). ``line_token`` places the picked line in a custom
    embed or a Components layout.
    """

    key: str
    noun: str
    tokens: tuple
    aliases: dict
    data_tokens: frozenset
    client_tokens: frozenset
    example: str

    @property
    def allow_key(self) -> str:
        return f"allow_member_{self.key}_messages"

    @property
    def variants_key(self) -> str:
        return f"{self.key}_message_variants"

    @property
    def as_embed_key(self) -> str:
        return f"{self.key}_message_as_embed_description"

    @property
    def line_token(self) -> str:
        return "{" + self.key + "_message}"

    @property
    def allowed_tokens(self) -> frozenset:
        return frozenset([doc["token"] for doc in self.tokens] + list(self.aliases))


_PLAYER_TOKEN = {"token": "{player_name}", "help": "Your name", "sample": "Zezima"}

MESSAGE_KINDS: dict = {
    MESSAGE_TYPE_DEATH: MessageKind(
        key=MESSAGE_TYPE_DEATH,
        noun="death",
        tokens=DEATH_TOKENS,
        aliases=DEATH_TOKEN_ALIASES,
        data_tokens=DEATH_DATA_TOKENS,
        client_tokens=_CLIENT_VALUE_TOKENS,
        example="{player_name} forgot to pray against {killer}",
    ),
    MESSAGE_TYPE_CLOG: MessageKind(
        key=MESSAGE_TYPE_CLOG,
        noun="collection log",
        tokens=(
            _PLAYER_TOKEN,
            {"token": "{item_name}", "help": "The item you logged", "sample": "Tanzanite fang"},
            {"token": "{source}", "help": "Where it came from", "sample": "Zulrah"},
            {"token": "{kill_count}", "help": "Your kill count there", "sample": "412"},
            {"token": "{slots_unlocked}", "help": "Your collection log slots", "sample": "1,024"},
            {"token": "{slots_total}", "help": "Collection log slots in the game", "sample": "1,692"},
        ),
        aliases={"{npc_name}": "{source}", "{kc_received}": "{kill_count}"},
        data_tokens=frozenset({
            "{source}", "{npc_name}", "{kill_count}", "{kc_received}",
            "{slots_unlocked}", "{slots_total}",
        }),
        client_tokens=frozenset({"{item_name}", "{source}", "{npc_name}", "{player_name_plain}"}),
        example="{player_name} finally saw a {item_name} after {kill_count} kills",
    ),
    MESSAGE_TYPE_DROP: MessageKind(
        key=MESSAGE_TYPE_DROP,
        noun="drop",
        tokens=(
            _PLAYER_TOKEN,
            {"token": "{item_name}", "help": "The item you got", "sample": "Twisted bow"},
            {"token": "{source}", "help": "What dropped it", "sample": "Great Olm"},
            {"token": "{value}", "help": "What it's worth", "sample": "1.2B"},
            {"token": "{quantity}", "help": "How many", "sample": "1"},
            {"token": "{kill_count}", "help": "Your kill count there", "sample": "57"},
        ),
        aliases={"{npc_name}": "{source}"},
        data_tokens=frozenset({"{source}", "{npc_name}", "{value}", "{quantity}", "{kill_count}"}),
        client_tokens=frozenset({"{item_name}", "{source}", "{npc_name}", "{player_name_plain}"}),
        example="{player_name} just made {value} off {source}",
    ),
    MESSAGE_TYPE_PB: MessageKind(
        key=MESSAGE_TYPE_PB,
        noun="personal best",
        tokens=(
            _PLAYER_TOKEN,
            {"token": "{boss}", "help": "The boss or activity", "sample": "Vorkath"},
            {"token": "{time}", "help": "Your new best time", "sample": "1:02.40"},
            {"token": "{team_size}", "help": "Team size", "sample": "Solo"},
            {"token": "{kill_count}", "help": "Your kill count there", "sample": "1,204"},
        ),
        aliases={"{npc_name}": "{boss}", "{personal_best}": "{time}"},
        data_tokens=frozenset({
            "{boss}", "{npc_name}", "{time}", "{personal_best}", "{team_size}", "{kill_count}",
        }),
        client_tokens=frozenset({"{boss}", "{npc_name}", "{team_size}", "{player_name_plain}"}),
        example="{player_name} shaved {boss} down to {time}",
    ),
}

#: Message types in the order editors list them.
MESSAGE_TYPES = (MESSAGE_TYPE_DEATH, MESSAGE_TYPE_CLOG, MESSAGE_TYPE_DROP, MESSAGE_TYPE_PB)


def message_kind(message_type) -> MessageKind:
    """The kind for a stored or requested type. Raises ``MemberMessageError``."""
    kind = MESSAGE_KINDS.get(str(message_type or MESSAGE_TYPE_DEATH))
    if kind is None:
        raise MemberMessageError("Unknown message type.")
    return kind

_TOKEN_RE = re.compile(r"\{[a-z0-9_]+\}", re.IGNORECASE)
_PING_RE = re.compile(r"@everyone|@here|<@[&!]?\d+>", re.IGNORECASE)
_DISCORD_ENTITY_RE = re.compile(
    r"@everyone|@here"
    r"|<@[&!]?\d+>"  # user and role mentions
    r"|<#\d+>"  # channel mentions
    r"|<a?:\w+:\d+>"  # custom emoji
    r"|</[^<>:\n]+:\d+>"  # slash-command mentions
    r"|<t:-?\d+(?::[a-z])?>",  # timestamps
    re.IGNORECASE,
)
_LINK_RE = re.compile(
    r"https?://\S*"
    r"|www\.\S*"
    r"|\bdiscord(?:app)?\.(?:gg|com/invite)\S*"
    r"|\b[a-z0-9][a-z0-9-]*\.(?:com|net|org|gg|io|co|me|xyz|tv|ly|link|site|app|dev|info|ru|uk|us)\b\S*",
    re.IGNORECASE,
)
# Discord renders these at the start of a line: headings, subtext, quotes.
_BLOCK_MARKDOWN_RE = re.compile(r"^\s*(?:#{1,3}\s|-#\s|>>>|>\s)")
# Control characters plus the Unicode line and paragraph separators.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")
_SPACES_RE = re.compile(r"\s{2,}")


class MemberMessageError(ValueError):
    """A message list that cannot be saved. ``str(error)`` is shown to the member."""


# ── Validation ────────────────────────────────────────────────────────────────

def message_issue(text: str, message_type: str = MESSAGE_TYPE_DEATH) -> str | None:
    """Why one (already trimmed) message cannot be saved, or ``None``."""
    kind = MESSAGE_KINDS.get(message_type) or MESSAGE_KINDS[MESSAGE_TYPE_DEATH]
    if len(text) > MAX_MESSAGE_LENGTH:
        return f"Each message can be at most {MAX_MESSAGE_LENGTH} characters."
    if _CONTROL_RE.search(text):
        return "Each message has to be a single line of text."
    if _DISCORD_ENTITY_RE.search(text):
        return "Messages can't mention people, roles or channels, or use custom emoji."
    if _LINK_RE.search(text):
        return "Messages can't contain links."
    if _BLOCK_MARKDOWN_RE.search(text):
        return "Messages can't start with a heading or a quote."
    unknown = sorted({
        token for token in _TOKEN_RE.findall(text)
        if token.lower() not in kind.allowed_tokens
    })
    if unknown:
        names = ", ".join(unknown)
        allowed = ", ".join(doc["token"] for doc in kind.tokens)
        plural = "s" if len(unknown) > 1 else ""
        return f"Unknown placeholder{plural} {names}. You can use {allowed}."
    return None


def normalize_messages(value, message_type: str = MESSAGE_TYPE_DEATH) -> list[str]:
    """Validate what a member submitted and return the list to store.

    Takes a list of strings (or the same as a JSON array string). Each message
    is trimmed; blank rows and exact duplicates are dropped rather than refused,
    because an editor's empty row means "no message" and a Discord modal has
    five optional boxes. Placeholder names are lowercased so ``{Killer}`` works.

    Raises :class:`MemberMessageError` with the first problem found.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise MemberMessageError("Messages must be a list of text lines.")
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise MemberMessageError("Messages must be a list of text lines.")
    # Refuse absurd payloads before touching every entry.
    if len(value) > MAX_MESSAGES * 4:
        raise MemberMessageError(f"You can save at most {MAX_MESSAGES} messages.")

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in value:
        text = _TOKEN_RE.sub(lambda m: m.group(0).lower(), raw.strip())
        if not text or text in seen:
            continue
        issue = message_issue(text, message_type)
        if issue:
            raise MemberMessageError(issue)
        seen.add(text)
        cleaned.append(text)
    if len(cleaned) > MAX_MESSAGES:
        raise MemberMessageError(f"You can save at most {MAX_MESSAGES} messages.")
    return cleaned


def parse_stored_messages(raw, message_type: str = MESSAGE_TYPE_DEATH) -> list[str]:
    """A stored ``messages`` value -> the messages that are valid today.

    Tolerant: bad JSON, non-strings and entries a newer rule refuses are all
    dropped rather than raised, so one damaged row can neither break a send nor
    get past the rules. A row edited by hand in the data browser counts as
    damaged if it breaks them.
    """
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if text and text not in out and message_issue(text, message_type) is None:
            out.append(text)
    return out[:MAX_MESSAGES]


# ── Sending ───────────────────────────────────────────────────────────────────

def sanitize_text(value) -> str:
    """Remove what a posted line must never carry: pings, Discord entities,
    links and line breaks. What remains is trimmed with its spaces collapsed."""
    text = _CONTROL_RE.sub(" ", "" if value is None else str(value))
    text = _DISCORD_ENTITY_RE.sub("", text)
    text = _LINK_RE.sub("", text)
    return _SPACES_RE.sub(" ", text).strip()


def _sanitize_member_template(template: str) -> str:
    text = sanitize_text(template)
    while _BLOCK_MARKDOWN_RE.search(text):
        text = _BLOCK_MARKDOWN_RE.sub("", text, count=1).strip()
    return text


def is_renderable(template: str, replacements: dict,
                  message_type: str = MESSAGE_TYPE_DEATH) -> bool:
    """False when the template names a detail this notification does not have."""
    kind = MESSAGE_KINDS.get(message_type) or MESSAGE_KINDS[MESSAGE_TYPE_DEATH]
    for token in _TOKEN_RE.findall(template or ""):
        if token in kind.data_tokens and not str(replacements.get(token) or "").strip():
            return False
    return True


def pick_template(templates, replacements: dict, rng=None,
                  message_type: str = MESSAGE_TYPE_DEATH) -> str | None:
    """A random template among those this notification can fill, or ``None``."""
    eligible = [
        t for t in (templates or [])
        if t and is_renderable(t, replacements, message_type)
    ]
    if not eligible:
        return None
    return (rng or random).choice(eligible)


def choose_template(member_templates, group_templates, replacements: dict, rng=None,
                    message_type: str = MESSAGE_TYPE_DEATH):
    """The template for this notification's message line, and where it came from.

    Returns ``(template, source)`` with source ``"member"``, ``"group"`` or
    ``"default"`` (template ``None``: the sender's own line, "has died!").
    ``member_templates`` must already be empty when the group does not allow
    member messages or has blocked this member —
    :func:`member_templates_for_group` does both checks.
    """
    template = pick_template(member_templates, replacements, rng, message_type)
    if template:
        return template, "member"
    template = pick_template(group_templates, replacements, rng, message_type)
    if template:
        return template, "group"
    return None, "default"


def choose_death_template(member_templates, group_templates, replacements: dict, rng=None):
    """:func:`choose_template` for deaths."""
    return choose_template(member_templates, group_templates, replacements, rng,
                           MESSAGE_TYPE_DEATH)


def render_template(template: str, replacements: dict, *, member: bool,
                    message_type: str = MESSAGE_TYPE_DEATH) -> str:
    """Fill a message template in one pass.

    One pass, not sequential ``str.replace``: a substituted value can never be
    read as a placeholder itself. ``member`` templates are sanitized first and
    may only use :data:`MEMBER_DEATH_TOKENS`; anything else is dropped, so a
    stored row cannot smuggle in ``{video_url}``. A group's own template keeps
    its unknown placeholders as written (how its editor documents them) and
    only loses pings. Client-sent values are cleaned either way.
    """
    kind = MESSAGE_KINDS.get(message_type) or MESSAGE_KINDS[MESSAGE_TYPE_DEATH]
    if member:
        text = _sanitize_member_template(template or "")
        allowed = kind.allowed_tokens
    else:
        text = _PING_RE.sub("", template or "")
        allowed = None

    def _sub(match):
        token = match.group(0)
        if allowed is not None and token not in allowed:
            return ""
        if token not in replacements:
            return "" if allowed is not None else token
        value = replacements[token]
        if token in kind.client_tokens:
            return sanitize_text(value)[:_CLIENT_VALUE_MAX_LENGTH]
        return "" if value is None else str(value)

    return _SPACES_RE.sub(" ", _TOKEN_RE.sub(_sub, text)).strip()


def sample_replacements(message_type: str = MESSAGE_TYPE_DEATH) -> dict:
    """The preview values the editors show, keyed like a real send."""
    kind = MESSAGE_KINDS.get(message_type) or MESSAGE_KINDS[MESSAGE_TYPE_DEATH]
    values = {doc["token"]: doc["sample"] for doc in kind.tokens}
    for alias, canonical in kind.aliases.items():
        values[alias] = values[canonical]
    return values


# ── Storage (session in, lazy model imports) ─────────────────────────────────

def group_allows_member_messages(session, group_id,
                                 message_type: str = MESSAGE_TYPE_DEATH) -> bool:
    """Whether a group posts members' own messages of this type. Fails closed."""
    from utils import group_config as gc

    try:
        kind = message_kind(message_type)
        return gc.is_truthy(gc.get(session, group_id, kind.allow_key))
    except Exception:
        return False


def group_allows_member_death_messages(session, group_id) -> bool:
    """Whether a group posts members' own death messages. Fails closed."""
    return group_allows_member_messages(session, group_id, MESSAGE_TYPE_DEATH)


def is_member_blocked(session, group_id, player_id) -> bool:
    from db.models import GroupMemberMessageBlock

    return (
        session.query(GroupMemberMessageBlock.id)
        .filter(
            GroupMemberMessageBlock.group_id == group_id,
            GroupMemberMessageBlock.player_id == player_id,
        )
        .first()
        is not None
    )


def load_member_message_row(session, player_id, message_type=MESSAGE_TYPE_DEATH):
    from db.models import PlayerCustomMessage

    return (
        session.query(PlayerCustomMessage)
        .filter(
            PlayerCustomMessage.player_id == player_id,
            PlayerCustomMessage.message_type == message_type,
        )
        .first()
    )


def load_member_messages(session, player_id, message_type=MESSAGE_TYPE_DEATH) -> list[str]:
    row = load_member_message_row(session, player_id, message_type)
    return parse_stored_messages(row.messages, message_type) if row is not None else []


def store_member_messages(
    session,
    player_id,
    messages,
    *,
    via: str,
    user_id=None,
    message_type=MESSAGE_TYPE_DEATH,
) -> list[str]:
    """Validate and save one account's messages; an empty list deletes the row.

    Raises :class:`MemberMessageError` before writing anything. Flushes but
    does not commit — the caller owns the transaction.
    """
    from db.models import PlayerCustomMessage

    message_kind(message_type)
    cleaned = normalize_messages(messages, message_type)
    row = load_member_message_row(session, player_id, message_type)
    if not cleaned:
        if row is not None:
            session.delete(row)
        session.flush()
        return []
    if row is None:
        row = PlayerCustomMessage(player_id=player_id, message_type=message_type)
        session.add(row)
    row.messages = json.dumps(cleaned, ensure_ascii=False)
    row.updated_via = via
    row.updated_by_user_id = user_id
    session.flush()
    return cleaned


def member_templates_for_group(session, group_id, player_id,
                               message_type: str = MESSAGE_TYPE_DEATH) -> list[str]:
    """This member's messages of this type, if this group will post them; else ``[]``.

    Every failure reads as "no member message": the group's own messages or the
    default line go out instead, and a blocked member is never let through by a
    database hiccup.
    """
    if group_id is None or player_id is None:
        return []
    try:
        if not group_allows_member_messages(session, group_id, message_type):
            return []
        if is_member_blocked(session, group_id, player_id):
            return []
        return load_member_messages(session, player_id, message_type)
    except Exception:
        return []


def member_death_templates_for_group(session, group_id, player_id) -> list[str]:
    """:func:`member_templates_for_group` for deaths."""
    return member_templates_for_group(session, group_id, player_id, MESSAGE_TYPE_DEATH)


def death_message_group_status(session, player_id) -> list[dict]:
    """:func:`member_message_group_status` for deaths."""
    return member_message_group_status(session, player_id, MESSAGE_TYPE_DEATH)


def member_message_group_status(session, player_id,
                                message_type: str = MESSAGE_TYPE_DEATH) -> list[dict]:
    """Every group this account belongs to, and whether its message of this
    type would be posted there: ``[{id, name, allowed, blocked}]``, by name."""
    allow_key = message_kind(message_type).allow_key
    from sqlalchemy import text

    from db.models import Group, GroupConfiguration, GroupMemberMessageBlock
    from utils import group_config as gc

    group_ids = [
        int(row[0])
        for row in session.execute(
            text("SELECT group_id FROM user_group_association WHERE player_id = :pid"),
            {"pid": player_id},
        ).all()
        if row[0] is not None
    ]
    if not group_ids:
        return []
    names = dict(
        session.query(Group.group_id, Group.group_name)
        .filter(Group.group_id.in_(group_ids))
        .all()
    )
    allowed = {
        int(group_id)
        for group_id, value in session.query(
            GroupConfiguration.group_id, GroupConfiguration.config_value
        )
        .filter(
            GroupConfiguration.group_id.in_(group_ids),
            GroupConfiguration.config_key == allow_key,
        )
        .all()
        if gc.is_truthy(value)
    }
    blocked = {
        int(group_id)
        for (group_id,) in session.query(GroupMemberMessageBlock.group_id)
        .filter(
            GroupMemberMessageBlock.player_id == player_id,
            GroupMemberMessageBlock.group_id.in_(group_ids),
        )
        .all()
    }
    status = [
        {
            "id": group_id,
            "name": names.get(group_id) or f"Group {group_id}",
            "allowed": group_id in allowed,
            "blocked": group_id in blocked,
        }
        for group_id in set(group_ids)
        if group_id in names
    ]
    status.sort(key=lambda g: (g["name"] or "").lower())
    return status


def limits_payload(message_type: str = MESSAGE_TYPE_DEATH) -> dict:
    """What every editor needs to know up front: the limits and the tokens.

    ``types`` lists every message type, so an editor can offer a picker
    without a copy of the list; ``type``/``tokens`` describe the one asked for.
    """
    kind = message_kind(message_type)
    return {
        "type": kind.key,
        "max_messages": MAX_MESSAGES,
        "max_length": MAX_MESSAGE_LENGTH,
        "tokens": [dict(doc) for doc in kind.tokens],
        "example": kind.example,
        "types": [
            {"type": k, "noun": MESSAGE_KINDS[k].noun} for k in MESSAGE_TYPES
        ],
    }
