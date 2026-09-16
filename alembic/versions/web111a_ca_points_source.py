"""A combat achievement point total that both of its sources can keep current.

``player_ca_varps.points`` was created with the state-sync tables (web99a) but
never written, so the Data API's ``combat_achievements.points`` was null for
every player. It now has two writers:

* the account sync, which counts the points its synced completion bits are
  worth; and
* every combat achievement completion, which carries the game's own total
  (varbit 14815) and arrives far more often than a sync does.

Two readings of one number need a rule for which to keep, and the rule needs
two facts per value (``db/ca_points.py`` has the rule itself):

* ``points_source`` - ``game`` or ``sync``: which reading set it.
* ``points_observed_at`` - when that reading was taken, so an in-game total
  that arrives late cannot overwrite a newer one.

``varps`` becomes nullable because a completion can now create the row for a
player who has never synced: they have a known total and no bits at all, and
an empty object would read as "synced, nothing completed".

Revision ID: web111a_ca_points_source
Revises: web110a_pb_loadout_model
"""
from alembic import op
import sqlalchemy as sa

revision = "web111a_ca_points_source"
down_revision = "web110a_pb_loadout_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "player_ca_varps", "varps", existing_type=sa.Text(), nullable=True
    )
    op.add_column(
        "player_ca_varps",
        sa.Column("points_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "player_ca_varps",
        sa.Column("points_observed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_ca_varps", "points_observed_at")
    op.drop_column("player_ca_varps", "points_source")
    # Rows with no bits exist only because of this revision: they hold a total
    # reported by a completion for a player who never synced. NOT NULL cannot
    # come back while they exist, and there is nothing else in them to keep.
    op.execute("DELETE FROM player_ca_varps WHERE varps IS NULL")
    op.alter_column(
        "player_ca_varps", "varps", existing_type=sa.Text(), nullable=False
    )
