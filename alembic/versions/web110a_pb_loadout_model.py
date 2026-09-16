"""Which character model a personal best was set in (web110a).

``personal_best_loadouts`` (P2.0) records the item ids a player wore and
carried at a personal best. The 3D character model is a separate upload keyed
by an outfit fingerprint (``player_state.model_fingerprint`` is only "the
outfit we hold right now"), so until now nothing tied a personal best to the
model that was worn for it. These two columns do:

* ``model_fingerprint`` - the outfit fingerprint to render for this time.
* ``model_source`` - how we know: ``kill`` when the plugin sent the
  fingerprint with the kill itself, ``recent`` when an older client sent
  none and the server recorded the outfit it most recently held for the
  player. The site labels the two differently, so the distinction has to be
  stored rather than guessed later.

Additive and nullable: existing rows (and clients that send nothing) simply
have no model.

Revision ID: web110a_pb_loadout_model
Revises: web109a_notification_always_list
"""
from alembic import op
import sqlalchemy as sa

revision = "web110a_pb_loadout_model"
down_revision = "web109a_notification_always_list"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personal_best_loadouts",
        sa.Column("model_fingerprint", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "personal_best_loadouts",
        sa.Column("model_source", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("personal_best_loadouts", "model_source")
    op.drop_column("personal_best_loadouts", "model_fingerprint")
