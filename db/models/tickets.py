from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Boolean, Text, Index, func
from db.models.base import Base

class Ticket(Base):
    __tablename__ = 'tickets'
    ticket_id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(String(255), nullable=False)
    type = Column(String(255), nullable=False)
    created_by = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    claimed_by = Column(Integer, ForeignKey('users.user_id'), nullable=True)
    # open -> (close_requested, set by the web admin dashboard; the webhook bot
    # archives the channel then flips it) -> closed
    status = Column(String(255), nullable=False)
    date_added = Column(DateTime, default=func.now())
    last_reply_uid = Column(String(255), nullable=True)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    # Archive metadata (web21a). subject is derived from the creator's first
    # message; date_closed/closed_by are stamped when the channel is archived.
    subject = Column(String(255), nullable=True)
    date_closed = Column(DateTime, nullable=True)
    closed_by = Column(Integer, ForeignKey('users.user_id'), nullable=True)
    # Inactivity auto-close (web67a). Set when the 5-day idle warning is posted;
    # cleared the moment a human replies (which restarts the 5-day clock). While
    # non-NULL the ticket is in the 24h grace window before auto-archive.
    inactivity_warned_at = Column(DateTime, nullable=True)


class TicketMessage(Base):
    """One mirrored Discord message inside a ticket channel (web21a).

    Rows are written live by the webhook bot's MessageCreate listener and
    upserted again from full channel history right before the channel is
    deleted, so a bot outage can't lose transcript content. The
    discord_message_id unique key is what makes that re-archive idempotent.
    Attachments are downloaded to static/assets/img/tickets/{ticket_id}/ and
    recorded in attachments_json as [{filename, path, content_type, size}]
    where path is relative to the /img/ route.
    """

    __tablename__ = 'ticket_messages'
    __table_args__ = (
        Index('idx_ticket_messages_ticket', 'ticket_id', 'date_sent'),
        Index('idx_ticket_messages_author', 'author_user_id'),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(Integer, ForeignKey('tickets.ticket_id'), nullable=False)
    discord_message_id = Column(String(32), nullable=True, unique=True)
    author_user_id = Column(Integer, ForeignKey('users.user_id'), nullable=True)
    author_discord_id = Column(String(32), nullable=False)
    author_name = Column(String(100), nullable=False)
    is_staff = Column(Boolean, nullable=False, default=False)
    is_bot = Column(Boolean, nullable=False, default=False)
    kind = Column(String(16), nullable=False, default='message')  # message|system
    content = Column(Text, nullable=True)
    attachments_json = Column(Text, nullable=True)
    date_sent = Column(DateTime, nullable=False)
    date_edited = Column(DateTime, nullable=True)
