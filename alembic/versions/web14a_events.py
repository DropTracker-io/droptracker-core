"""Events system (backend Task 14).

The web platform's Phase-6 events feature. Tables are namespaced ``web_*`` to
avoid colliding with the pre-existing legacy events system (``events`` /
``event_tasks`` / ``event_teams`` / ``event_participants`` / … ), which has a
different, actively-used schema and is left untouched.
"""

from alembic import op
import sqlalchemy as sa


revision = "web14a_events"
down_revision = "web11a_subs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("has_bingo", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_web_event_group_status", "web_events", ["group_id", "status"], unique=False)

    op.create_table(
        "web_event_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("target", sa.String(length=120), nullable=True),
        sa.Column("target_value", sa.Integer(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["event_id"], ["web_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_web_event_task_event", "web_event_tasks", ["event_id"], unique=False)

    op.create_table(
        "web_event_teams",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["event_id"], ["web_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_web_event_team_event", "web_event_teams", ["event_id"], unique=False)

    op.create_table(
        "web_event_team_members",
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["web_event_teams.id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("team_id", "player_id"),
    )

    op.create_table(
        "web_event_bingo_cells",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["web_events.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["web_event_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_web_bingo_cell_event", "web_event_bingo_cells", ["event_id"], unique=False)

    op.create_table(
        "web_event_bingo_completions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cell_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["cell_id"], ["web_event_bingo_cells.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["web_event_teams.id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_web_bingo_completion_cell", "web_event_bingo_completions", ["cell_id"], unique=False)


def downgrade() -> None:
    op.drop_table("web_event_bingo_completions")
    op.drop_table("web_event_bingo_cells")
    op.drop_table("web_event_team_members")
    op.drop_table("web_event_teams")
    op.drop_table("web_event_tasks")
    op.drop_index("idx_web_event_group_status", table_name="web_events")
    op.drop_table("web_events")
