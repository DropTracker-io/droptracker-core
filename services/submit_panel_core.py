"""Pure pieces of the in-Discord Submit button (suggestion #198).

Members send a drop, collection log slot, personal best, combat achievement or
pet from a button instead of remembering ``/submit``:

    Submit button (``dtsubmit:open``)        -> ephemeral "what are you submitting?"
    type picker   (``dtsubmit:type``)        -> a modal with that type's fields,
                                                an account picker when the member
                                                has several, and a screenshot upload
    modal submit  (``dtsubmit:modal:<type>``) -> the same intake /submit uses

The button lives on the Activity launcher card (``submit_on_launch_card``) and/or
its own standing card (``submit_panel_channel``). The modal is sent as raw JSON
because it uses Discord's Label + File Upload modal components, which the
bundled interactions.py predates; :func:`parse_modal_submit` reads both the new
Label shape and the classic ActionRow shape.

Nothing here touches Discord or the database, so it is unit-tested directly
(``services/submit_panel_discord.py`` is the shell).
"""
from __future__ import annotations

import hashlib
from typing import Optional

SUBMIT_BUTTON_ID = "dtsubmit:open"
SUBMIT_BUTTON_LABEL = "Submit"
TYPE_SELECT_ID = "dtsubmit:type"
MODAL_PREFIX = "dtsubmit:modal:"

#: The standalone Submit card's settings and bookkeeping rows.
PANEL_CHANNEL_KEY = "submit_panel_channel"
PANEL_MESSAGE_KEY = "submit_panel_message"
PANEL_REF_KEY = "submit_panel_message_id"
PANEL_VARIANT_KEY = "submit_panel_variant"

DEFAULT_PANEL_MESSAGE = (
    "Got a drop, a personal best or a collection log slot the plugin didn't catch? "
    "Tap **Submit**, fill in what you got and add your screenshot."
)

#: (type, label, description) for the type picker, in display order.
TYPE_CHOICES = (
    ("drop", "Drop", "An item you received"),
    ("clog", "Collection log slot", "A new collection log unlock"),
    ("pb", "Personal best", "A new best kill time"),
    ("ca", "Combat achievement", "A task you completed"),
    ("pet", "Pet", "A pet you received"),
)
TYPE_LABELS = {key: label for key, label, _d in TYPE_CHOICES}

CA_TIERS = ("easy", "medium", "hard", "elite", "master", "grandmaster")

PROOF_FIELD = "proof"
ACCOUNT_FIELD = "account"

# Discord component types used in the raw modal.
_TEXT_INPUT, _STRING_SELECT, _LABEL, _FILE_UPLOAD = 4, 3, 18, 19
_SHORT = 1
_MAX_SELECT_OPTIONS = 25


def panel_variant(message: Optional[str]) -> str:
    """Short fingerprint of the card's text: a changed message edits the card."""
    text = (message or "").strip() or DEFAULT_PANEL_MESSAGE
    return "m" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def panel_text(message: Optional[str]) -> str:
    return (message or "").strip() or DEFAULT_PANEL_MESSAGE


def modal_custom_id(sub_type: str) -> str:
    return f"{MODAL_PREFIX}{sub_type}"


def modal_type(custom_id: str) -> Optional[str]:
    """The submission type a modal custom_id carries, or None."""
    if not isinstance(custom_id, str) or not custom_id.startswith(MODAL_PREFIX):
        return None
    sub_type = custom_id[len(MODAL_PREFIX):]
    return sub_type if sub_type in TYPE_LABELS else None


def _text(custom_id, label, *, required, placeholder=None, max_length=100, description=None):
    component = {"type": _TEXT_INPUT, "custom_id": custom_id, "style": _SHORT,
                 "required": required, "max_length": max_length}
    if placeholder:
        component["placeholder"] = placeholder
    field = {"type": _LABEL, "label": label, "component": component}
    if description:
        field["description"] = description
    return field


def _select(custom_id, label, options, *, description=None):
    field = {
        "type": _LABEL,
        "label": label,
        "component": {
            "type": _STRING_SELECT,
            "custom_id": custom_id,
            "required": True,
            "min_values": 1,
            "max_values": 1,
            "options": options[:_MAX_SELECT_OPTIONS],
        },
    }
    if description:
        field["description"] = description
    return field


def _type_fields(sub_type: str) -> list:
    if sub_type == "drop":
        return [
            _text("item", "Item", required=True, placeholder="Abyssal whip"),
            _text("npc", "Where it came from", required=True, placeholder="Abyssal demon"),
            _text("quantity", "Quantity", required=False, placeholder="1", max_length=10),
        ]
    if sub_type == "clog":
        return [
            _text("item", "Collection log item", required=True, placeholder="Dragon pickaxe"),
            _text("npc", "Where it came from", required=False, placeholder="Callisto"),
            _text("kc", "Kill count", required=False, max_length=10),
        ]
    if sub_type == "pb":
        return [
            _text("npc", "Boss", required=True, placeholder="Vorkath"),
            _text("time", "Kill time", required=True, placeholder="1:23.4", max_length=12),
            _text("team_size", "Team size", required=False, placeholder="1", max_length=3),
        ]
    if sub_type == "ca":
        return [
            _text("task", "Task", required=True, placeholder="Faithless Crypt Run"),
            _select("tier", "Tier", [{"label": t.title(), "value": t} for t in CA_TIERS]),
        ]
    if sub_type == "pet":
        return [
            _text("item", "Pet", required=True, placeholder="Pet chaos elemental"),
            _text("npc", "Where it came from", required=False, placeholder="Chaos Elemental"),
            _text("kc", "Kill count", required=False, max_length=10),
        ]
    raise ValueError(f"unknown submission type {sub_type!r}")


def build_modal(sub_type: str, accounts: list, *, with_upload: bool = True) -> dict:
    """The raw modal (interaction callback ``data``) for one submission type.

    ``accounts`` are the member's claimed RSNs: with more than one, an account
    picker comes first. Discord allows five top-level components; every type
    has at most three fields, plus the account picker and the screenshot.
    """
    components = []
    names = [str(a) for a in accounts if a]
    if len(names) > 1:
        components.append(_select(
            ACCOUNT_FIELD, "Account",
            [{"label": n[:100], "value": n[:100]} for n in names]))
    components.extend(_type_fields(sub_type))
    if with_upload:
        components.append({
            "type": _LABEL,
            "label": "Screenshot",
            "description": "Optional, but your clan's staff may ask for one.",
            "component": {"type": _FILE_UPLOAD, "custom_id": PROOF_FIELD,
                          "min_values": 0, "max_values": 1, "required": False},
        })
    return {
        "custom_id": modal_custom_id(sub_type),
        "title": f"Submit a {TYPE_LABELS[sub_type].lower()}"[:45],
        "components": components[:5],
    }


def parse_modal_submit(data: dict) -> tuple[dict, list]:
    """``(values, attachments)`` from a MODAL_SUBMIT interaction's ``data``.

    ``values`` maps custom_id -> text (a select's first choice). ``attachments``
    are the uploaded files' resolved attachment dicts (url, content_type,
    size, filename). Handles Label-wrapped components (``component``) and the
    classic ActionRow shape (``components``).
    """
    values: dict = {}
    attachment_ids: list = []

    def _take(component):
        if not isinstance(component, dict):
            return
        custom_id = component.get("custom_id")
        kind = component.get("type")
        if not custom_id:
            return
        if kind == _FILE_UPLOAD:
            attachment_ids.extend(str(v) for v in (component.get("values") or []))
        elif "values" in component:
            chosen = component.get("values") or []
            values[custom_id] = str(chosen[0]) if chosen else ""
        else:
            values[custom_id] = str(component.get("value") or "")

    for top in (data or {}).get("components") or []:
        if not isinstance(top, dict):
            continue
        if isinstance(top.get("component"), dict):
            _take(top["component"])
        for child in top.get("components") or []:
            _take(child)

    resolved = ((data or {}).get("resolved") or {}).get("attachments") or {}
    attachments = [resolved[a] for a in attachment_ids if isinstance(resolved.get(a), dict)]
    return values, attachments


def _int_or_none(raw, name: str, minimum: int = 0):
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return None
    if not raw.isdigit():
        raise ValueError(f"{name} must be a whole number.")
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return value


def payload_fields(sub_type: str, values: dict, parse_kill_time_ms) -> dict:
    """Keyword arguments for ``build_manual_payload`` from modal values.
    Raises ValueError with a member-facing message."""
    item = (values.get("item") or "").strip() or None
    npc = (values.get("npc") or "").strip() or None
    if sub_type == "drop":
        return {"item_name": item, "npc_name": npc,
                "quantity": _int_or_none(values.get("quantity"), "Quantity", 1)}
    if sub_type in ("clog", "pet"):
        return {"item_name": item, "npc_name": npc,
                "kc": _int_or_none(values.get("kc"), "Kill count")}
    if sub_type == "pb":
        time_ms = parse_kill_time_ms(values.get("time"))
        if not time_ms:
            raise ValueError("Enter the kill time like 1:23.4.")
        return {"npc_name": npc, "time_ms": time_ms,
                "team_size": _int_or_none(values.get("team_size"), "Team size", 1)}
    if sub_type == "ca":
        tier = (values.get("tier") or "").strip().lower()
        if tier not in CA_TIERS:
            raise ValueError("Pick the task's tier.")
        return {"task": (values.get("task") or "").strip() or None, "tier": tier}
    raise ValueError("Unknown submission type.")


def interaction_user_id(payload: dict) -> Optional[str]:
    """The Discord id of whoever sent a raw interaction payload.

    interactions.py parses the same dict our raw listener gets, and its
    ``Member._process_dict`` pops ``member["user"]`` and leaves ``member["id"]``
    behind. Which listener runs first is not fixed, so read both shapes.
    """
    member = (payload or {}).get("member") or {}
    user = member.get("user") or (payload or {}).get("user") or {}
    found = user.get("id") or member.get("id")
    return str(found) if found else None


def has_label_components(components) -> bool:
    """True when a modal's components use the Label shape (``component``)."""
    return any(isinstance(top, dict) and isinstance(top.get("component"), dict)
               for top in components or [])


def flatten_modal_components(components) -> list:
    """Label-wrapped modal components -> the ActionRow shape interactions.py
    expects (``[{"components": [{"custom_id", "value"}]}]``)."""
    flat = []
    for top in components or []:
        inner = []
        if isinstance(top, dict) and isinstance(top.get("component"), dict):
            inner = [top["component"]]
        elif isinstance(top, dict):
            inner = list(top.get("components") or [])
        for comp in inner:
            if not isinstance(comp, dict) or not comp.get("custom_id"):
                continue
            value = comp.get("value")
            if value is None:
                chosen = comp.get("values") or []
                value = ",".join(str(v) for v in chosen)
            flat.append({"type": 1, "components": [
                {"type": comp.get("type"), "custom_id": comp["custom_id"], "value": value}]})
    return flat
