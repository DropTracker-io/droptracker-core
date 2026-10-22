"""GP value columns INT -> BIGINT (web142a).

Items can now be worth more than 2,147,483,647 gp (RuneLite 1.13 prices are
64-bit; the Obligator trades at 3.3-5b), and every such drop was rejected
because nothing past the 32-bit cap could be stored.

``drops`` is far too big for a plain ALTER: on MariaDB 10.11 an INT -> BIGINT
change is ALGORITHM=COPY only, which blocks writes for the whole rewrite. On
prod it is converted online with pt-online-schema-change first, and this
migration then skips it. On small databases (dev, CI, fresh installs) it is
altered here directly. A large INT ``drops`` aborts the migration rather than
locking the table.

Revision ID: web142a_drop_value_bigint
Revises: web141a_group_verification_codes
"""
import sqlalchemy as sa
from alembic import op

revision = "web142a_drop_value_bigint"
down_revision = "web141a_group_verification_codes"
branch_labels = None
depends_on = None

# (table, column, column definition after the type)
_COLUMNS = [
    ("drops", "value", "NULL DEFAULT NULL"),
    ("drop_splits", "split_value", "NOT NULL"),
    ("group_recent_drops", "value", "NOT NULL"),
    ("item_value_overrides", "flat_bonus", "NOT NULL DEFAULT 0"),
    ("item_value_overrides", "fallback_value", "NOT NULL DEFAULT 0"),
]

# Above this many rows an INT drops.value must be converted online, not here.
_DROPS_INLINE_MAX_ROWS = 5_000_000


def _column_type(bind, table, column):
    return bind.execute(sa.text(
        "SELECT DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
    ), {"t": table, "c": column}).scalar()


def _modify(bind, table, column, sql_type, definition):
    bind.execute(sa.text(
        f"ALTER TABLE `{table}` MODIFY `{column}` {sql_type} {definition}"
    ))


def upgrade() -> None:
    bind = op.get_bind()
    for table, column, definition in _COLUMNS:
        current = _column_type(bind, table, column)
        if current is None or current == "bigint":
            continue
        if table == "drops":
            rows = bind.execute(sa.text(
                "SELECT TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'drops'"
            )).scalar() or 0
            if rows > _DROPS_INLINE_MAX_ROWS:
                raise RuntimeError(
                    f"drops has ~{rows} rows and drops.value is still {current}: "
                    "convert it online with pt-online-schema-change first "
                    "(MODIFY value BIGINT NULL DEFAULT NULL), then re-run."
                )
        _modify(bind, table, column, "BIGINT", definition)


def downgrade() -> None:
    # Narrowing fails (strict mode) or truncates if any value exceeds INT, and
    # drops is too big to rewrite here; only the small tables are reverted.
    bind = op.get_bind()
    for table, column, definition in _COLUMNS:
        if table == "drops":
            continue
        if _column_type(bind, table, column) == "bigint":
            _modify(bind, table, column, "INT(11)", definition)
