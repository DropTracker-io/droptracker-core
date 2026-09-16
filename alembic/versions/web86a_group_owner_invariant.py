"""Enforce one owner per group in group_admins (web86a).

The owner/admin split gives the single owner exclusive control of the admin
roster; that only means anything if a group cannot quietly acquire a second
owner (via the /admin/data row editor, a stray script, or a bug in the
handover path). MySQL/MariaDB has no partial indexes, so the invariant is
expressed as a UNIQUE index over a VIRTUAL generated column that is NULL for
every non-owner row — NULLs don't collide, so N admins are fine and a second
owner is rejected at the engine.

The column is deliberately absent from the SQLAlchemy model: MariaDB computes
it and nothing in the app reads it.

PRECONDITION: run `python -m scripts.migrate_group_single_owner --apply`
first. 46 groups carry duplicate owner grants from the original
seed_group_admins rollout and this index will refuse to build over them.

Revision ID: web86a_group_owner_invariant
Revises: web85a_known_issues
"""
from alembic import op
import sqlalchemy as sa

revision = "web86a_group_owner_invariant"
down_revision = "web85a_known_issues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    dupes = conn.execute(
        sa.text(
            "SELECT group_id, COUNT(*) c FROM group_admins "
            "WHERE role = 'owner' GROUP BY group_id HAVING c > 1"
        )
    ).fetchall()
    if dupes:
        listed = ", ".join(f"group {g} ({c} owners)" for g, c in dupes[:10])
        more = "" if len(dupes) <= 10 else f" (+{len(dupes) - 10} more)"
        raise RuntimeError(
            f"{len(dupes)} group(s) still have multiple owner grants: {listed}{more}. "
            "Run `python -m scripts.migrate_group_single_owner --apply` before "
            "this migration."
        )

    op.execute(
        "ALTER TABLE group_admins "
        "ADD COLUMN owner_group_id INT "
        "AS (IF(role = 'owner', group_id, NULL)) VIRTUAL"
    )
    op.execute(
        "ALTER TABLE group_admins "
        "ADD UNIQUE KEY uix_group_single_owner (owner_group_id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE group_admins DROP INDEX uix_group_single_owner")
    op.execute("ALTER TABLE group_admins DROP COLUMN owner_group_id")
