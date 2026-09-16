"""Persisted monthly/annual recap payloads (web77a).

One row per (scope, subject, period) holding the fully-computed card data as
JSON. Persisted rather than cached, for three reasons that each rule out a TTL:

* the annual recap is a fold over twelve monthly rows, so those rows have to
  outlive any cache;
* `/groups/{id}/recap/{period}` is a permanent public URL — a recap that
  evaporates is worse than one that never existed;
* the biggest-drop card references a screenshot that
  `droptracker-prune-images.timer` deletes once it is 30 days old and worth
  under 1M GP. Freezing the payload captures the URL while the file still
  exists.

`schema_version` lets later versions add cards without invalidating older rows —
recaps are an archive, so the rule is add, never swap.

Revision ID: web77a_recap_snapshots
Revises: web76a_drop_kill_count
"""
from alembic import op
import sqlalchemy as sa

revision = "web77a_recap_snapshots"
down_revision = "web76a_drop_kill_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recap_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        # 'group' | 'player'. Plain string rather than an ENUM so adding a scope
        # later is a code change, not a table rebuild on a growing table.
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Integer, nullable=False),
        # 'YYYY-MM' for a month, 'YYYY' for a year — sorts chronologically as a
        # string either way, and the length distinguishes the two.
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("payload", sa.Text(length=4294967295), nullable=False),  # LONGTEXT
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("generated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    # The idempotency guard: a cycle re-run for a period it has already done
    # updates in place instead of inserting a duplicate.
    op.create_unique_constraint(
        "uq_recap_scope_subject_period",
        "recap_snapshots",
        ["scope", "subject_id", "period"],
    )
    # "every group's 2026-07" — the cycle's own sweep, and the archive index on
    # a subject's profile reads the same index from the other direction.
    op.create_index(
        "idx_recap_period_scope", "recap_snapshots", ["period", "scope"]
    )


def downgrade() -> None:
    op.drop_index("idx_recap_period_scope", table_name="recap_snapshots")
    op.drop_constraint(
        "uq_recap_scope_subject_period", "recap_snapshots", type_="unique"
    )
    op.drop_table("recap_snapshots")
