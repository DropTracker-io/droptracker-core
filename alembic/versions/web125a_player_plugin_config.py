"""player_plugin_config (web125a).

``player_plugin_config``  the latest plugin settings snapshot per player
                          (config_snapshot submissions, plugin 6.0.16+),
                          plus the one before it.

A new table only, so it is safe to apply live. Guarded, so a re-run is
harmless.

Revision ID: web125a_player_plugin_config
Revises: web124a_deepest_delve
"""
from alembic import op
import sqlalchemy as sa

revision = "web125a_player_plugin_config"
down_revision = "web124a_deepest_delve"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("player_plugin_config"):
        return
    op.create_table(
        "player_plugin_config",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=True),
        sa.Column("plugin_version", sa.String(32), nullable=True),
        sa.Column("runelite_version", sa.String(32), nullable=True),
        sa.Column("used_api", sa.Boolean(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("previous_config_json", sa.Text(), nullable=True),
        sa.Column("previous_captured_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("player_id"),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
    )


def downgrade():
    op.drop_table("player_plugin_config")
