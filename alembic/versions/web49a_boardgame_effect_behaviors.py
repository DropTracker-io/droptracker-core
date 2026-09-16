"""Board-game effect behaviors (web49a): the generalized shop-item effect
framework + the Dinh's Bulwark rework.

Schema:
- ``web_event_board_positions.blocked_until_turn`` (Integer, NULL) — the
  turn counter target a "blocked" team must reach before rolling again
  (roadblock stall_turns; served one consumed roll attempt at a time in
  services/boardgame_engine._serve_blocked_turn).

Data:
- Dinh's Bulwark (key ``dinhs_bulwark``): activate it (its handler now
  implements the full spec) and reshape ``effect_config`` into the resolved
  behavior form the effect registry (services/boardgame_effects.py) layers
  and snapshots: {"break_on": "pass", "stall_turns": 1, "visible_to_all":
  true}. break_on governs consumption only — passing over the bulwark
  always stops the mover (the placer included; no immunity).

Revision ID: web49a_boardgame_effect_behaviors
Revises: web48a_leadership_pergroup_discord
"""
from alembic import op
import sqlalchemy as sa

revision = "web49a_boardgame_effect_behaviors"
down_revision = "web48a_leadership_pergroup_discord"
branch_labels = None
depends_on = None

_NEW_CONFIG = '{"break_on": "pass", "stall_turns": 1, "visible_to_all": true}'
_NEW_DESCR = ("Place a bulwark on a tile — any team that tries to pass it "
              "(yours included) is stopped there and loses a turn.")
# The web45a seed values, restored on downgrade.
_OLD_CONFIG = '{"stall_turns": 1}'
_OLD_DESCR = ("Place a roadblock on a tile — the next team to pass loses "
              "a turn.")


def upgrade() -> None:
    op.add_column(
        "web_event_board_positions",
        sa.Column("blocked_until_turn", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE web_boardgame_shop_items "
            "SET effect_config = :cfg, description = :descr, active = 1 "
            "WHERE `key` = 'dinhs_bulwark'"
        ).bindparams(cfg=_NEW_CONFIG, descr=_NEW_DESCR)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE web_boardgame_shop_items "
            "SET effect_config = :cfg, description = :descr, active = 0 "
            "WHERE `key` = 'dinhs_bulwark'"
        ).bindparams(cfg=_OLD_CONFIG, descr=_OLD_DESCR)
    )
    op.drop_column("web_event_board_positions", "blocked_until_turn")
