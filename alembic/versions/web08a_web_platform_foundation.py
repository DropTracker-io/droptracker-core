"""Web platform foundation (backend Task 08).

Adds the tables and user preference columns the first-party Next.js front-end
needs (FRONTEND_PLAN.md §13):

  * ``users`` preference/staff columns (Task 03/12).
  * ``group_admins``  - explicit web-granted admin rights (§7.2).
  * ``announcements`` - announcements feature (§10).
  * ``audit_log``     - config/admin action trail (§11, §15).

Session/OAuth-state tables are intentionally not created: the Web API uses
stateless JWT sessions with a Redis deny-list (see ``web_api/session.py``).
``xf_user_id`` is left untouched (§7.4).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "web08a_foundation"
down_revision = "a9d60bddb823"
branch_labels = None
depends_on = None


_USER_COLUMNS = [
    ("dm_on_rank_change", sa.Boolean(), sa.text("0")),
    ("dm_on_points", sa.Boolean(), sa.text("0")),
    ("update_logs_opt_in", sa.Boolean(), sa.text("0")),
    ("patreon_group", sa.Integer(), None),
    ("premium_group", sa.Integer(), None),
    ("is_superadmin", sa.Boolean(), sa.text("0")),
]


def _existing_columns(table: str) -> set:
    bind = op.get_bind()
    insp = inspect(bind)
    try:
        return {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return set()


def upgrade() -> None:
    # --- users preference/staff columns (idempotent: skip if present) ---
    existing = _existing_columns("users")
    for name, coltype, default in _USER_COLUMNS:
        if name in existing:
            continue
        kwargs = {"nullable": True}
        if default is not None:
            kwargs["server_default"] = default
        op.add_column("users", sa.Column(name, coltype, **kwargs))

    # --- group_admins ---
    op.create_table(
        "group_admins",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="admin"),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["granted_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uix_group_admin"),
    )
    op.create_index("idx_group_admin_group", "group_admins", ["group_id"], unique=False)

    # --- announcements ---
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False, server_default="group"),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("cover_image_url", sa.String(length=512), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("discord_message_id", sa.String(length=32), nullable=True),
        sa.Column("discord_channel_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_announcement_scope",
        "announcements",
        ["scope_type", "group_id", "published_at"],
        unique=False,
    )

    # --- audit_log ---
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=True),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_group_created", "audit_log", ["group_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_audit_group_created", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("idx_announcement_scope", table_name="announcements")
    op.drop_table("announcements")

    op.drop_index("idx_group_admin_group", table_name="group_admins")
    op.drop_table("group_admins")

    existing = _existing_columns("users")
    for name, _coltype, _default in reversed(_USER_COLUMNS):
        if name in existing:
            op.drop_column("users", name)
