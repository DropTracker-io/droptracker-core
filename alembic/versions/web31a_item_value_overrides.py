"""item_value_overrides: runtime-editable "component of X, worth Y" valuation rules.

Moves the hard-coded valuation special-cases out of utils/ge_value.py (and the
duplicated id lists in /value_mods and the GitHub Pages valued_items.txt) into a
DB table superadmins can edit at runtime without a service restart. See
db/models/item_value_override.py. Rows are seeded by
scripts/seed_item_value_overrides.py (not here) so the seed stays idempotent and
re-runnable as new items are added.
"""

from alembic import op
import sqlalchemy as sa


revision = "web31a_item_value_overrides"
down_revision = "web30a_event_guilds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_value_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("item_name", sa.String(length=125), nullable=False),
        sa.Column("divisor", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("flat_bonus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback_value", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("components", sa.Text(), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", name="uix_item_value_override_item_id"),
    )
    op.create_index("idx_item_value_override_name", "item_value_overrides", ["item_name"])


def downgrade() -> None:
    op.drop_index("idx_item_value_override_name", table_name="item_value_overrides")
    op.drop_table("item_value_overrides")
