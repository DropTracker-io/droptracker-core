"""Manual submission policy (suggestion #45), Phase 1.

- drops.source — intake-path marker: NULL = RuneLite plugin, 'manual' =
  website manual submit. Nullable, no default → INSTANT ALTER on MariaDB.
- drop_group_moderation — per-(drop, group) moderation state; rows exist only
  when a group's ``manual_submission_policy`` withheld a manual drop from that
  group (status 'excluded' in Phase 1; 'pending'/'approved'/'rejected' arrive
  with the Phase-2 review queue). See db/models/drop_moderation.py.

The policy itself is a group_configurations key (``manual_submission_policy``,
registry-validated) — no schema change needed for it.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "web37a_manual_submission_policy"
down_revision = "web36a_event_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("drops", sa.Column("source", sa.String(length=16), nullable=True))
    op.create_table(
        "drop_group_moderation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("drop_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="excluded"),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["drop_id"], ["drops.drop_id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_drop_group_moderation", "drop_group_moderation",
        ["drop_id", "group_id"], unique=True,
    )
    op.create_index(
        "idx_drop_moderation_group_status", "drop_group_moderation",
        ["group_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("drop_group_moderation")
    op.drop_column("drops", "source")
