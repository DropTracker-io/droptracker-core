"""Index group_configurations by (config_key, group_id) (web98a).

``group_configurations`` carried exactly one secondary index, on ``group_id``.
That serves "all config for one group" fine, but it leaves two hot shapes slow:

* ``WHERE config_key IN (...)`` across every group -- no usable index at all,
  so it degraded to a full table scan (``type=ALL``, ~17k rows, ~7ms). The
  lootboard poster's prelude in ``bots/main.py`` runs exactly this shape every
  8 minutes.
* ``WHERE group_id = ? AND config_key = ?`` -- the single most common lookup in
  the codebase. It could only use the ``group_id`` prefix and then filtered
  ~83 candidate rows per hit to return one.

The composite is deliberately ``(config_key, group_id)`` and not the reverse:
``group_id`` already has its own index, so a ``(group_id, config_key)`` index
would only duplicate that prefix. Leading with ``config_key`` covers the
cross-group scan *and* still resolves the per-group lookup on the full key.

Note ``(group_id, config_key)`` is NOT unique in practice -- there are real
duplicate rows -- so this index is non-unique on purpose. Making it unique
would fail on existing data.

**Run this with a short lock_wait_timeout.** The app keeps long-lived
idle-in-transaction connections (observed: 12 open transactions, the oldest
idle ~55 minutes). Any of them that touched this table holds a shared metadata
lock, so the exclusive MDL this DDL needs may never be grantable -- and while
the request is pending, every *new* query against ``group_configurations``
queues behind it. An unbounded attempt therefore stalls config reads fleet-wide
rather than just waiting its turn. Failing fast and retrying is correct here;
bounding the blast radius matters more than landing on the first try.

Revision ID: web98a_group_config_key_index
Revises: web97a_player_account_type
"""
from alembic import op

revision = "web98a_group_config_key_index"
down_revision = "web97a_player_account_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Bound the blast radius: if the exclusive metadata lock is not grantable
    # within a few seconds, abort instead of parking a pending MDL request that
    # blocks every other reader of this table. See the module docstring.
    op.execute("SET SESSION lock_wait_timeout = 5")
    op.create_index(
        "ix_group_configurations_config_key_group_id",
        "group_configurations",
        ["config_key", "group_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_group_configurations_config_key_group_id",
        table_name="group_configurations",
    )
