"""AuditLog.event_id — event-scoped audit trail (web57a).

The audit_log table was only group-scoped, and event.* rows encode their
event inconsistently in `target` (`web_event_completions.{id}`,
`web_events.{id}`, or a bare `{event_id}` for board actions), so there was
no reliable way to list one event's admin actions. The manager audit log
(GET /events/{id}/audit) needs exactly that, so we add a nullable, indexed
`event_id` column and set it at every event-scoped write site going forward.

Deliberately NOT a foreign key: an event hard-delete must neither be blocked
by nor cascade away its own durable audit trail, and library/template/config
actions legitimately leave it NULL.

Best-effort backfill of existing rows resolves the three unambiguous target
shapes; older rows whose event can't be derived stay NULL.

Revision ID: web57a_auditlog_event_id
Revises: web56a_users_discord_id_unique
"""
from alembic import op
import sqlalchemy as sa

revision = "web57a_auditlog_event_id"
down_revision = "web56a_users_discord_id_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("event_id", sa.Integer(), nullable=True))
    op.create_index(
        "idx_audit_event_created", "audit_log", ["event_id", "created_at"]
    )

    # Best-effort backfill (unambiguous target shapes only).
    conn = op.get_bind()
    # 1. Completion/award/revoke rows → target 'web_event_completions.{id}'.
    conn.execute(sa.text(
        "UPDATE audit_log a "
        "JOIN web_event_completions c "
        "  ON a.target = CONCAT('web_event_completions.', c.id) "
        "SET a.event_id = c.event_id "
        "WHERE a.event_id IS NULL "
        "  AND a.action IN ('event.completion.confirm','event.completion.reject',"
        "'event.award','event.revoke')"
    ))
    # 2. Rows whose target is 'web_events.{id}'.
    conn.execute(sa.text(
        "UPDATE audit_log a "
        "JOIN web_events e ON a.target = CONCAT('web_events.', e.id) "
        "SET a.event_id = e.id "
        "WHERE a.event_id IS NULL AND a.action LIKE 'event.%'"
    ))
    # 3. Board/loot-sweep rows whose target is a bare '{event_id}'. Restricted
    #    to the actions we know use a bare event id, so a team/task id target
    #    (always prefixed) can never be mis-resolved here.
    conn.execute(sa.text(
        "UPDATE audit_log a "
        "JOIN web_events e ON a.target = CAST(e.id AS CHAR) "
        "SET a.event_id = e.id "
        "WHERE a.event_id IS NULL "
        "  AND (a.action LIKE 'event.board.%' OR a.action = 'event.loot_sweep.image')"
    ))


def downgrade() -> None:
    op.drop_index("idx_audit_event_created", table_name="audit_log")
    op.drop_column("audit_log", "event_id")
