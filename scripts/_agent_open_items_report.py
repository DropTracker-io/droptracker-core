"""Read-only one-off: dump open suggestions/bug reports + open tickets as JSON.

Not part of the maintained scripts/ toolkit — ad hoc report for a Discord
review session, safe to delete.
"""
import sys
import json

sys.path.insert(0, ".")

from db import Session
from db.models import Suggestion, Ticket, SuggestionMessage, TicketMessage

s = Session()
try:
    suggestions = (
        s.query(Suggestion)
        .filter(Suggestion.is_open.is_(True))
        .order_by(Suggestion.last_activity_at.desc())
        .all()
    )
    out_suggestions = []
    for sug in suggestions:
        msg_count = (
            s.query(SuggestionMessage)
            .filter(SuggestionMessage.suggestion_id == sug.id)
            .count()
        )
        out_suggestions.append({
            "id": sug.id,
            "type": sug.type,
            "title": sug.title,
            "body_md": sug.body_md,
            "author_name": sug.author_name,
            "origin": sug.origin,
            "status": sug.status,
            "message_count": sug.message_count if sug.message_count is not None else msg_count,
            "discord_thread_id": sug.discord_thread_id,
            "created_at": sug.created_at.isoformat() if sug.created_at else None,
            "last_activity_at": sug.last_activity_at.isoformat() if sug.last_activity_at else None,
        })

    tickets = (
        s.query(Ticket)
        .filter(Ticket.status.in_(["open", "close_requested"]))
        .order_by(Ticket.date_added.desc())
        .all()
    )
    out_tickets = []
    for t in tickets:
        first_msg = (
            s.query(TicketMessage)
            .filter(TicketMessage.ticket_id == t.ticket_id)
            .order_by(TicketMessage.date_sent.asc())
            .first()
        )
        msg_count = (
            s.query(TicketMessage)
            .filter(TicketMessage.ticket_id == t.ticket_id)
            .count()
        )
        out_tickets.append({
            "ticket_id": t.ticket_id,
            "type": t.type,
            "subject": t.subject,
            "status": t.status,
            "channel_id": t.channel_id,
            "date_added": t.date_added.isoformat() if t.date_added else None,
            "date_updated": t.date_updated.isoformat() if t.date_updated else None,
            "inactivity_warned_at": t.inactivity_warned_at.isoformat() if t.inactivity_warned_at else None,
            "message_count": msg_count,
            "first_message": (first_msg.content[:600] if first_msg and first_msg.content else None),
            "first_author": (first_msg.author_name if first_msg else None),
        })

    print(json.dumps({"suggestions": out_suggestions, "tickets": out_tickets}, default=str))
finally:
    s.close()
