"""Data API: re-scale the tier budgets to the measured cost model.

The original weights were ordered by intuition on a 1-8 scale and the budgets
were picked to match. Measuring every loader over 100 real players showed the
true spread is 475:1, so the weights were re-scaled to ~0.05 ms of server work
per player and the budgets have to move with them or every request becomes
unaffordable.

Sized from the owner's actual use case: a clan pulling *everything* about a
400-member roster. That is 4 pages of 100 (the page cap bounds per-request
memory) at 34,400 units each = 137,600. The entry tier is 200,000 rather than
the 137,600 that "fits", because a budget sized to exactly clear its target
case has no room for a retry: measured live, one earlier call in the same
minute was enough to make the fourth page 429. 45% headroom absorbs that. A
cheap sweep — identity and loot for the same 400 — is 800.

Revision ID: dapi2_recalibrate_tiers
Revises: dapi1_api_keys
"""
import sqlalchemy as sa
from alembic import op

revision = "dapi2_recalibrate_tiers"
down_revision = "dapi1_api_keys"
branch_labels = None
depends_on = None

# (tier, requests_per_min, cost_units_per_min, requests_per_day, max_concurrency)
_NEW = [
    ("standard", 60, 200_000, 10_000, 4),
    ("elevated", 300, 800_000, 100_000, 8),
    ("partner", 1_200, 3_000_000, 1_000_000, 16),
]
_OLD = [
    ("standard", 60, 300, 10_000, 4),
    ("elevated", 300, 2_000, 100_000, 8),
    ("partner", 1_200, 10_000, 1_000_000, 16),
]


def _apply(rows) -> None:
    for tier, per_min, cost, per_day, concurrency in rows:
        op.execute(
            sa.text(
                "UPDATE api_key_tiers SET requests_per_min = :per_min, "
                "cost_units_per_min = :cost, requests_per_day = :per_day, "
                "max_concurrency = :concurrency WHERE tier_key = :tier"
            ).bindparams(
                per_min=per_min, cost=cost, per_day=per_day,
                concurrency=concurrency, tier=tier,
            )
        )


def upgrade() -> None:
    _apply(_NEW)


def downgrade() -> None:
    _apply(_OLD)
