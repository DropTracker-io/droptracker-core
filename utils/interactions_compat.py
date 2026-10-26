"""Make interactions.py tolerate Discord enum values it does not know yet.

Discord adds values to its enums before the library does. interactions.py
converts them with a plain ``Enum(value)``, which raises, and the raise happens
inside its gateway event processors: the whole event is dropped. Since
2026-10-25 Discord sends embeds of type ``"components"``, so a reaction or an
edit on such a message crashed ``_on_raw_message_reaction_add`` /
``_on_raw_message_update`` with ``'components' is not a valid EmbedType``
(Sentry).

``install()`` gives the affected enums a ``_missing_`` that returns a
stand-in member carrying the raw value (named ``UNKNOWN_<VALUE>``), so the
event parses and nothing that reads ``embed.type`` breaks. Idempotent; every
bot entry point calls it before connecting.
"""
from __future__ import annotations

_PATCHED = False


def _tolerant_missing(cls, value):
    if not isinstance(value, str):
        return None
    member = object.__new__(cls)
    member._name_ = f"UNKNOWN_{value.upper()}"
    member._value_ = value
    return member


def install() -> None:
    global _PATCHED
    if _PATCHED:
        return
    from interactions.models.discord.enums import EmbedType

    EmbedType._missing_ = classmethod(_tolerant_missing)
    _PATCHED = True
