"""Group verification codes (web141a).

A WOM-style ``123-456-789`` code that proves, inside the RuneLite plugin, that
the person at the keyboard was given the code by the group's owner. The plugin
identifies a player only by name + account hash, which travel in plain query
strings and are no secret, so plugin-side group administration needs a second
factor that never touches those channels.

One row per group. Only a keyed hash is stored (``db/group_verification_codes``
holds the scheme); the plaintext exists exactly once, in the response that
issued it. ``code_hash`` NULL means "no active code": the group has never had
one, or staff cleared it, and either way the owner may reveal a fresh one.

``generation`` counts issues. Anything later minted from a code (plugin device
sessions) records the generation it was minted under, so issuing a new code or
clearing the old one revokes all of them without a sweep.
"""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from .base import Base


class GroupVerificationCode(Base):
    __tablename__ = "group_verification_codes"
    __table_args__ = {"extend_existing": True}

    group_id = Column(
        Integer, ForeignKey("groups.group_id", ondelete="CASCADE"), primary_key=True
    )
    code_hash = Column(String(80), nullable=True)
    generation = Column(Integer, nullable=False, default=0)

    #: How the active code came to be: 'create' | 'reveal' | 'reset'.
    issued_via = Column(String(16), nullable=True)
    issued_at = Column(DateTime, nullable=True)
    issued_by_user_id = Column(Integer, nullable=True)

    #: Last time the code was withdrawn without a replacement (staff clear,
    #: ownership transfer). Informational; ``code_hash`` is the state.
    cleared_at = Column(DateTime, nullable=True)
    cleared_by_user_id = Column(Integer, nullable=True)
    cleared_reason = Column(String(32), nullable=True)
