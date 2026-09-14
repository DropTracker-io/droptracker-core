"""Members' own notification messages, and a group's per-member block.

A player writes the line their clan sees when they die ("{player_name} forgot
to pray against {killer}") on the website, in Discord (``/settings``) or in the
RuneLite plugin. Nothing a member writes reaches a channel the group has not
opened to it: the group turns the feature on with
``allow_member_death_messages`` and can block individual members.

These are just the rows. What a valid message is, which line wins, and how it
renders all live in ``db.member_messages``, which every surface imports.
"""

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

from .base import Base


class PlayerCustomMessage(Base):
    """One account's messages of one kind (``message_type``, only 'death' so far).

    Keyed by account rather than by Discord user: a main and its ironman alt are
    different characters, and it is the account that dies. ``messages`` is a
    JSON array of message templates, one picked at random per notification.
    A player who clears every message has no row at all.
    """

    __tablename__ = "player_custom_messages"
    __table_args__ = (
        UniqueConstraint("player_id", "message_type", name="uix_player_custom_message"),
        {"extend_existing": True},
    )

    # A surrogate key, not (player_id, message_type): the superadmin data
    # browser addresses rows by one column, and staff clear abusive rows there.
    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    message_type = Column(String(16), nullable=False)
    messages = Column(Text, nullable=False)
    # Who last wrote the row and through which surface ('web', 'discord',
    # 'plugin', 'staff'). The plugin authenticates by account, not by user,
    # so its writes carry no user id.
    updated_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    updated_via = Column(String(8), nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class GroupMemberMessageBlock(Base):
    """A group's "don't post this member's own messages in our channels".

    Covers every kind of member message, not one type: a leader blocking
    someone for what they wrote on their deaths would not expect the same
    person's collection log line to keep appearing. The member's message is
    untouched and still shows in any other group that allows it.
    """

    __tablename__ = "group_member_message_blocks"
    __table_args__ = (
        UniqueConstraint("group_id", "player_id", name="uix_group_member_message_block"),
        Index("idx_member_message_block_player", "player_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
