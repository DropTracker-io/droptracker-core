"""Event numeric type widenings — make the hand-ALTERs reproducible (web58a).

The ORM has declared web_event_teams.score and web_event_progress.progress as
DOUBLE (loot_sweep receipts score fractional 2-dp points; loot_value tasks
fold raw GP) since 2026-07-19, but prod was widened by hand and no migration
performed the change — so DR / a fresh region / dt_migrate_test comes up with
INTEGER columns that silently truncate fractional scores (a decayed 1-pt
receipt = 0.8 -> 0 -> wrong winning team). web_event_completions.quantity was
declared BigInteger at the same time but the hand-ALTER was MISSED entirely:
prod still has int(11), so a single ledger row > 2^31-1 (one big GP-stack
drop on a loot_value task) raises an uncaught DataError and dead-letters the
envelope.

All three ALTERs are idempotent in effect: on prod the first two are no-op
rebuilds to the type the column already has; on a schema built purely from
migrations they perform the real widening. Tables are small (tens of
thousands of rows at most) — sub-second.

Revision ID: web58a_event_numeric_types
Revises: web57a_auditlog_event_id
"""
from alembic import op
import sqlalchemy as sa

revision = "web58a_event_numeric_types"
down_revision = "web57a_auditlog_event_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "web_event_teams", "score",
        type_=sa.Float(53), existing_nullable=False, existing_server_default="0",
    )
    op.alter_column(
        "web_event_progress", "progress",
        type_=sa.Float(53), existing_nullable=False, existing_server_default="0",
    )
    op.alter_column(
        "web_event_completions", "quantity",
        type_=sa.BigInteger(), existing_nullable=False, existing_server_default="1",
    )


def downgrade() -> None:
    # Narrowing back can silently truncate fractional scores / big stacks —
    # only for scratch DBs.
    op.alter_column(
        "web_event_completions", "quantity",
        type_=sa.Integer(), existing_nullable=False, existing_server_default="1",
    )
    op.alter_column(
        "web_event_progress", "progress",
        type_=sa.BigInteger(), existing_nullable=False, existing_server_default="0",
    )
    op.alter_column(
        "web_event_teams", "score",
        type_=sa.Integer(), existing_nullable=False, existing_server_default="0",
    )
