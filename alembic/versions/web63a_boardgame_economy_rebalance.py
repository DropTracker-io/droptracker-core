"""Board-game shop: economy rebalance + retire dominated duplicates (EC1/SH8).

Data-only. Two economy power-ups could never repay their cost at the default
coin ladder, and two items were strictly dominated (identical effect + type,
higher price) so no rational player would ever buy them:

- ``coin_chest`` (boost_coins): 80c for a ×2 next-task reward tops out at +50
  on a fire task — always a loss. Now 40c and ×3 so it pays off on the tasks
  worth boosting.
- ``gloves_of_silence`` (coin_toll): 160c tolling 25/rival needed ~7 passed
  teams to break even. Now 80c tolling 40/rival — break-even at two.
- ``ghommals_penny`` (reroll_task, same tier) was dominated by ``reroll_scroll``
  (cheaper, shorter cooldown); ``pirate_petes_parrot`` (steal_item) was
  dominated by ``mischievous_rat``. Both retired (active=0) to keep the shop
  legible — every listed item is now a distinct choice.

Revision ID: web63a_boardgame_economy_rebalance
Revises: web62a_board_shop_next_refresh
"""
from alembic import op
import sqlalchemy as sa

revision = "web63a_boardgame_economy_rebalance"
down_revision = "web62a_board_shop_next_refresh"
branch_labels = None
depends_on = None

_CHEST_DESCR_NEW = "Triple the coins from your next completed task."
_CHEST_DESCR_OLD = "Double the coins from your next completed task."
_DOMINATED = ("ghommals_penny", "pirate_petes_parrot")


def upgrade() -> None:
    op.execute(sa.text(
        "UPDATE web_boardgame_shop_items "
        "SET cost_coins = 40, effect_config = :cfg, description = :d "
        "WHERE `key` = 'coin_chest'"
    ).bindparams(cfg='{"multiplier": 3}', d=_CHEST_DESCR_NEW))
    op.execute(sa.text(
        "UPDATE web_boardgame_shop_items "
        "SET cost_coins = 80, effect_config = :cfg "
        "WHERE `key` = 'gloves_of_silence'"
    ).bindparams(cfg='{"coins_per_team": 40}'))
    op.execute(sa.text(
        "UPDATE web_boardgame_shop_items SET active = 0 WHERE `key` IN :keys"
    ).bindparams(sa.bindparam("keys", _DOMINATED, expanding=True)))


def downgrade() -> None:
    op.execute(sa.text(
        "UPDATE web_boardgame_shop_items "
        "SET cost_coins = 80, effect_config = :cfg, description = :d "
        "WHERE `key` = 'coin_chest'"
    ).bindparams(cfg='{"multiplier": 2}', d=_CHEST_DESCR_OLD))
    op.execute(sa.text(
        "UPDATE web_boardgame_shop_items "
        "SET cost_coins = 160, effect_config = :cfg "
        "WHERE `key` = 'gloves_of_silence'"
    ).bindparams(cfg='{"coins_per_team": 25}'))
    op.execute(sa.text(
        "UPDATE web_boardgame_shop_items SET active = 1 WHERE `key` IN :keys"
    ).bindparams(sa.bindparam("keys", _DOMINATED, expanding=True)))
