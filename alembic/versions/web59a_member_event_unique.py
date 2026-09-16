"""One team per event as a DB constraint (web59a, audit P0-9).

The "one team per event" invariant was enforced only in Python (query the
membership, 409 if present, insert otherwise) — two concurrent joins (double
click / mobile retry) both see no membership and both insert; in auto-assign
mode they can even pick two different smallest teams, silently double-crediting
every drop the player contributes. The composite PK (team_id, player_id) can't
catch that because the two rows are on different teams.

Denormalize event_id onto web_event_team_members (backfilled from the team),
make it NOT NULL, and add UNIQUE (event_id, player_id) so the losing insert
fails at the DB. Writers all set event_id from web58a-era code; the join
routes map the IntegrityError to a clean 409.

Verified before authoring: prod has zero players on two teams of one event.

Revision ID: web59a_member_event_unique
Revises: web58a_event_numeric_types
"""
from alembic import op
import sqlalchemy as sa

revision = "web59a_member_event_unique"
down_revision = "web58a_event_numeric_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_team_members",
        sa.Column("event_id", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE web_event_team_members m "
        "JOIN web_event_teams t ON t.id = m.team_id "
        "SET m.event_id = t.event_id"
    )
    op.alter_column(
        "web_event_team_members", "event_id",
        existing_type=sa.Integer(), nullable=False,
    )
    op.create_foreign_key(
        "fk_web_evt_member_event", "web_event_team_members",
        "web_events", ["event_id"], ["id"],
    )
    op.create_index(
        "uq_web_evt_member_event_player", "web_event_team_members",
        ["event_id", "player_id"], unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_web_evt_member_event_player", table_name="web_event_team_members")
    op.drop_constraint(
        "fk_web_evt_member_event", "web_event_team_members", type_="foreignkey")
    op.drop_column("web_event_team_members", "event_id")
