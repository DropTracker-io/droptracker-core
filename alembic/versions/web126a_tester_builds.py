"""plugin_test_downloads + player_plugin_versions (web126a).

``plugin_test_downloads``   one row per download of a tester client build
                            from the website.
``player_plugin_versions``  the first and latest time each player submitted
                            with each plugin version, and whether that
                            version was ahead of the Plugin Hub release when
                            they first ran it.

New tables only, so it is safe to apply live. Each is guarded, so a re-run
is harmless.

Revision ID: web126a_tester_builds
Revises: web125a_player_plugin_config
"""
from alembic import op
import sqlalchemy as sa

revision = "web126a_tester_builds"
down_revision = "web125a_player_plugin_config"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("plugin_test_downloads"):
        op.create_table(
            "plugin_test_downloads",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("build_id", sa.String(96), nullable=False),
            sa.Column("plugin_version", sa.String(32), nullable=True),
            sa.Column("commit_sha", sa.String(40), nullable=True),
            sa.Column("runelite_version", sa.String(32), nullable=True),
            sa.Column("file_name", sa.String(160), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        )
        op.create_index("idx_ptd_user_created", "plugin_test_downloads",
                        ["user_id", "created_at"])
        op.create_index("idx_ptd_build", "plugin_test_downloads", ["build_id"])

    if not inspector.has_table("player_plugin_versions"):
        op.create_table(
            "player_plugin_versions",
            sa.Column("player_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.String(32), nullable=False),
            sa.Column("first_seen", sa.DateTime(), nullable=False),
            sa.Column("last_seen", sa.DateTime(), nullable=False),
            sa.Column("sightings", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("prerelease", sa.Boolean(), nullable=False,
                      server_default=sa.text("0")),
            sa.PrimaryKeyConstraint("player_id", "version"),
            sa.ForeignKeyConstraint(["player_id"], ["players.player_id"],
                                    ondelete="CASCADE"),
        )
        op.create_index("idx_ppv_version_seen", "player_plugin_versions",
                        ["version", "last_seen"])


def downgrade():
    op.drop_table("player_plugin_versions")
    op.drop_table("plugin_test_downloads")
