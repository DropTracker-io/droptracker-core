"""web69a: widen event task target_value to BIGINT.

``web_event_tasks.target_value`` (and its library mirror) was INT, so any goal
past signed-INT max reached MySQL as error 1264 and surfaced as a 500 rather
than a validation message. A user hit this creating a ``loot_value`` task for
5,000,000,000 GP ("Obtain 5b in Value from All Drops"). loot_value is the only
kind that can realistically exceed INT — every other goal is a kill count, a
level, or a per-skill XP total (capped at 200M).

BIGINT matches the columns these goals are actually compared against —
``web_event_completions.quantity`` (BigInteger, P1-7) and
``web_event_progress.progress`` (DOUBLE) — so the whole loot_value path is now
consistently wide. Validation gained a matching JS-safe ceiling
(``MAX_TARGET_VALUE``) so out-of-range input 422s instead of 500-ing.

Widening is non-lossy, and both tables are small (~250 rows each), so this is a
fast in-place alter. The downgrade narrows back to INT and will fail loudly if
any row now exceeds INT range — deliberate, rather than silently truncating.
"""

import sqlalchemy as sa
from alembic import op

revision = "web69a_task_target_value_bigint"
down_revision = "web68a_live_event_edits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("web_event_tasks", "web_event_task_library"):
        op.alter_column(
            table,
            "target_value",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    for table in ("web_event_tasks", "web_event_task_library"):
        op.alter_column(
            table,
            "target_value",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
