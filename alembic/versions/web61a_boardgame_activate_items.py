"""Board-game shop: activate the interference items whose handlers shipped.

Data-only. The web45a seed inserted the P3 interference set inactive
(``active = 0``) "until handlers land". Their handlers have since shipped —
``advance`` (Teleport tablet), ``freeze_opponent`` (Ice barrage) and ``shield``
(Spirit shield) are all fully implemented in services/boardgame_shop.py — but no
migration ever flipped them on, so on a clean ``alembic upgrade head`` those
three effects are unbuyable (S1). ``dinhs_bulwark`` was already activated by
web49a; this finishes the set.

Idempotent: prod was hand-activated, so the UPDATE is a no-op there.

Revision ID: web61a_boardgame_activate_items
Revises: web60a_player_ehb
"""
from alembic import op
import sqlalchemy as sa

revision = "web61a_boardgame_activate_items"
down_revision = "web60a_player_ehb"
branch_labels = None
depends_on = None

# Keys whose handlers + registry specs are live (services/boardgame_shop.py,
# services/boardgame_effects.py) but whose seed rows still ship inactive.
_ACTIVATE_KEYS = ("teleport_tablet", "ice_barrage", "spirit_shield")


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE web_boardgame_shop_items SET active = 1 "
            "WHERE `key` IN :keys"
        ).bindparams(sa.bindparam("keys", _ACTIVATE_KEYS, expanding=True))
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE web_boardgame_shop_items SET active = 0 "
            "WHERE `key` IN :keys"
        ).bindparams(sa.bindparam("keys", _ACTIVATE_KEYS, expanding=True))
    )
