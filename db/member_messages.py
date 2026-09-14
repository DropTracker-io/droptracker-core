"""Members' own death messages — the rules every surface shares.

A member writes the line their clan sees when they die ("{player_name} forgot
to pray against {killer}"). Four places read or write those messages, and all
of them import from here so they cannot disagree about what a valid message is
or which line gets posted:

  * ``web_api/routes/member_messages.py`` — the website editor for members and
    the moderation card for group leaders;
  * ``api/routes/member_messages.py`` — the RuneLite plugin's editor;
  * ``services/player_settings_panel.py`` — ``/settings`` in Discord;
  * ``services/notification_service.py`` — the death sender, which picks the line.

**Groups opt in.** Nothing a member writes reaches a channel unless the group
set ``allow_member_death_messages``, and a leader can still block individual
members (``group_member_message_blocks``). It is off by default because turning
it on gives every member a line in the clan's channel, and that is the group's
decision to make, not the member's.

**Which line wins**, per group and per death: the member's own message when the
group allows it and has not blocked them, else the group's randomized messages,
else "{player_name} has died!". A message whose placeholders come out blank for
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

#: ``player_custom_messages.message_type`` for death messages.
MESSAGE_TYPE_DEATH = "death"

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

def message_issue(text: str) -> str | None:
    """Why one (already trimmed) message cannot be saved, or ``None``."""
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
        if token.lower() not in MEMBER_DEATH_TOKENS
    })
    if unknown:
        names = ", ".join(unknown)
        allowed = ", ".join(doc["token"] for doc in DEATH_TOKENS)
        plural = "s" if len(unknown) > 1 else ""
        return f"Unknown placeholder{plural} {names}. You can use {allowed}."
    return None


def normalize_messages(value) -> list[str]:
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
        issue = message_issue(text)
        if issue:
            raise MemberMessageError(issue)
        seen.add(text)
        cleaned.append(text)
    if len(cleaned) > MAX_MESSAGES:
        raise MemberMessageError(f"You can save at most {MAX_MESSAGES} messages.")
    return cleaned


def parse_stored_messages(raw) -> list[str]:
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
        if text and text not in out and message_issue(text) is None:
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


def is_renderable(template: str, replacements: dict) -> bool:
    """False when the template names a death detail this death does not have."""
    for token in _TOKEN_RE.findall(template or ""):
        if token in DEATH_DATA_TOKENS and not str(replacements.get(token) or "").strip():
            return False
    return True


def pick_template(templates, replacements: dict, rng=None) -> str | None:
    """A random template among those this death can fill, or ``None``."""
    eligible = [t for t in (templates or []) if t and is_renderable(t, replacements)]
    if not eligible:
        return None
    return (rng or random).choice(eligible)


def choose_death_template(member_templates, group_templates, replacements: dict, rng=None):
    """The template for this death's message line, and where it came from.

    Returns ``(template, source)`` with source ``"member"``, ``"group"`` or
    ``"default"`` (template ``None``: the sender's own "has died!" line).
    ``member_templates`` must already be empty when the group does not allow
    member messages or has blocked this member —
    :func:`member_death_templates_for_group` does both checks.
    """
    template = pick_template(member_templates, replacements, rng)
    if template:
        return template, "member"
    template = pick_template(group_templates, replacements, rng)
    if template:
        return template, "group"
    return None, "default"


def render_template(template: str, replacements: dict, *, member: bool) -> str:
    """Fill a message template in one pass.

    One pass, not sequential ``str.replace``: a substituted value can never be
    read as a placeholder itself. ``member`` templates are sanitized first and
    may only use :data:`MEMBER_DEATH_TOKENS`; anything else is dropped, so a
    stored row cannot smuggle in ``{video_url}``. A group's own template keeps
    its unknown placeholders as written (how its editor documents them) and
    only loses pings. Client-sent values are cleaned either way.
    """
    if member:
        text = _sanitize_member_template(template or "")
        allowed = MEMBER_DEATH_TOKENS
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
        if token in _CLIENT_VALUE_TOKENS:
            return sanitize_text(value)[:_CLIENT_VALUE_MAX_LENGTH]
        return "" if value is None else str(value)

    return _SPACES_RE.sub(" ", _TOKEN_RE.sub(_sub, text)).strip()


def sample_replacements() -> dict:
    """The preview values the editors show, keyed like a real send."""
    values = {doc["token"]: doc["sample"] for doc in DEATH_TOKENS}
    for alias, canonical in DEATH_TOKEN_ALIASES.items():
        values[alias] = values[canonical]
    return values


# ── Storage (session in, lazy model imports) ─────────────────────────────────

def group_allows_member_death_messages(session, group_id) -> bool:
    """Whether a group posts members' own death messages. Fails closed."""
    from utils import group_config as gc

    try:
        return gc.is_truthy(gc.get(session, group_id, ALLOW_DEATH_CONFIG_KEY))
    except Exception:
        return False


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
    return parse_stored_messages(row.messages) if row is not None else []


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

    cleaned = normalize_messages(messages)
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


def member_death_templates_for_group(session, group_id, player_id) -> list[str]:
    """This member's death messages, if this group will post them; else ``[]``.

    Every failure reads as "no member message": the group's own messages or the
    default line go out instead, and a blocked member is never let through by a
    database hiccup.
    """
    if group_id is None or player_id is None:
        return []
    try:
        if not group_allows_member_death_messages(session, group_id):
            return []
        if is_member_blocked(session, group_id, player_id):
            return []
        return load_member_messages(session, player_id)
    except Exception:
        return []


def death_message_group_status(session, player_id) -> list[dict]:
    """Every group this account belongs to, and whether its death message
    would be posted there: ``[{id, name, allowed, blocked}]``, by name."""
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
            GroupConfiguration.config_key == ALLOW_DEATH_CONFIG_KEY,
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


def limits_payload() -> dict:
    """What every editor needs to know up front: the limits and the tokens."""
    return {
        "max_messages": MAX_MESSAGES,
        "max_length": MAX_MESSAGE_LENGTH,
        "tokens": [dict(doc) for doc in DEATH_TOKENS],
    }
