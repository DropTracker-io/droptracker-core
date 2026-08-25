"""Per-group notification blacklist: items/NPCs a clan never wants announced (web101a).

Group leaders curate a list of items ("Bones") and NPCs ("Barrows") whose
submissions are recorded and scored as normal but never posted to that group's
Discord channels. One table, small per group, read on the notification hot path
by ``group_id`` — hence the plain secondary index.

``match_key`` stores the normalized comparison form (``db.notification_blacklist``)
rather than recomputing it at read time, so the uniqueness rule and the pipeline
lookup agree by construction: "Twisted Bow", "twisted bow" and "Twisted_bow" are
one entry, not three.

``game_id`` is intentionally NOT a foreign key to ``items``/``npc_list``: an
entry may be typed in for a name the catalog does not carry yet, and the id is
only ever used to render an icon.

Revision ID: web101a_notification_blacklist
Revises: web100a_manifest_payload_longtext
"""
from alembic import op
import sqlalchemy as sa

revision = "web101a_notification_blacklist"
down_revision = "web100a_manifest_payload_longtext"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_notification_blacklist",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(length=8), nullable=False),
        sa.Column("entry_name", sa.String(length=125), nullable=False),
        sa.Column("match_key", sa.String(length=125), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=True),
        sa.Column("added_by_user_id", sa.Integer(), nullable=True),
        sa.Column("date_added", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("date_updated", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "entry_type", "match_key", name="uix_group_blacklist_entry"
        ),
    )
    op.create_index(
        "idx_group_blacklist_group", "group_notification_blacklist", ["group_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_group_blacklist_group", table_name="group_notification_blacklist")
    op.drop_table("group_notification_blacklist")
