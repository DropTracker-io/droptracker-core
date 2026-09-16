"""Data API: explicit key scope, adding 'global' for third-party integrations.

A partner site tying into our data needs to read every group and every player,
which neither of the existing scopes can express.

The scope is a *column* rather than being inferred from "no owner set",
because inference makes an all-access key the result of forgetting to set an
owner — any bug on the mint path would silently produce one. With an explicit
value the constraint rejects that insert instead.

Existing rows are backfilled from the ownership they already have, so nothing
gains or loses access here.

Revision ID: dapi3_key_scope
Revises: dapi2_recalibrate_tiers
"""
import sqlalchemy as sa
from alembic import op

revision = "dapi3_key_scope"
down_revision = "dapi2_recalibrate_tiers"
branch_labels = None
depends_on = None

_CHECK = (
    "(scope = 'user'   AND owner_user_id IS NOT NULL AND group_id IS NULL)"
    " OR (scope = 'group'  AND group_id IS NOT NULL AND owner_user_id IS NULL)"
    " OR (scope = 'global' AND owner_user_id IS NULL AND group_id IS NULL)"
)


def upgrade() -> None:
    # Nullable first so the backfill has somewhere to land, then tightened.
    op.add_column("api_keys", sa.Column("scope", sa.String(16), nullable=True))
    op.execute(
        "UPDATE api_keys SET scope = CASE "
        "WHEN owner_user_id IS NOT NULL THEN 'user' ELSE 'group' END "
        "WHERE scope IS NULL"
    )
    op.alter_column("api_keys", "scope", existing_type=sa.String(16),
                    nullable=False, server_default="group")

    # The old constraint said "exactly one owner", which 'global' violates by
    # design. Replace rather than add: both cannot hold at once.
    try:
        op.drop_constraint("ck_api_keys_one_owner", "api_keys", type_="check")
    except Exception:
        # MariaDB names CHECKs inconsistently across versions and the original
        # may have been created inline; a missing one is not a failure.
        pass
    op.create_check_constraint("ck_api_keys_scope_owner", "api_keys", _CHECK)


def downgrade() -> None:
    # Global keys cannot be represented without the column, so they go.
    op.execute("DELETE FROM api_keys WHERE scope = 'global'")
    try:
        op.drop_constraint("ck_api_keys_scope_owner", "api_keys", type_="check")
    except Exception:
        pass
    op.create_check_constraint(
        "ck_api_keys_one_owner", "api_keys",
        "(owner_user_id IS NULL) != (group_id IS NULL)",
    )
    op.drop_column("api_keys", "scope")
