"""Board-game shop expansion (web50a): new effects + per-event shop config.

Schema:
- ``web_event_shop_rotation`` gains OVERRIDE columns (a row now overrides the
  catalog defaults instead of being an allow-list subset):
  ``enabled`` (Boolean), ``stock_per_refresh`` (Integer, NULL = unlimited),
  ``per_team_cap`` (Integer, NULL = uncapped).
- ``web_event_board_config`` gains ``shop_refreshed_at`` / ``shop_refreshed_turn``
  (the stock-refresh clock; services/boardgame_shop.maybe_refresh_shop).
- ``web_event_board_positions`` gains ``pending_choice`` (JSON list of choose_task
  candidates awaiting a pick).
- new index ``idx_web_evt_inv_item`` on web_event_team_inventory for per-team
  purchase-cap counting.

Data: 15 new catalog power-ups (choose_task, reroll_move, steal_item, ward,
reroll_opponent_task, extra_dice, choose_roll, reroll_task variants, coin_toll,
knockback, cleanse). All active.

Revision ID: web50a_boardgame_shop_expansion
Revises: web49a_boardgame_effect_behaviors
"""
from alembic import op
import sqlalchemy as sa

revision = "web50a_boardgame_shop_expansion"
down_revision = "web49a_boardgame_effect_behaviors"
branch_labels = None
depends_on = None

# Keys of the rows this migration seeds (downgrade removes exactly these).
_SEED_KEYS = (
    "cache_of_runes", "binding_necklace", "enchanted_die", "mischievous_rat",
    "pirate_petes_parrot", "rat_poison", "ward_scroll", "intricate_pouch",
    "super_strength", "wizards_mind_bomb", "ghommals_penny", "escape_crystal",
    "gloves_of_silence", "bandos_godsword", "prayer_potion",
)


def upgrade() -> None:
    # -- Per-event shop overrides ------------------------------------------
    op.add_column(
        "web_event_shop_rotation",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "web_event_shop_rotation",
        sa.Column("stock_per_refresh", sa.Integer(), nullable=True),
    )
    op.add_column(
        "web_event_shop_rotation",
        sa.Column("per_team_cap", sa.Integer(), nullable=True),
    )

    # -- Shop refresh clock -------------------------------------------------
    op.add_column(
        "web_event_board_config",
        sa.Column("shop_refreshed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "web_event_board_config",
        sa.Column("shop_refreshed_turn", sa.Integer(), nullable=True),
    )

    # -- choose_task pending pick ------------------------------------------
    op.add_column(
        "web_event_board_positions",
        sa.Column("pending_choice", sa.Text(), nullable=True),
    )

    # -- Per-team purchase-cap counting index ------------------------------
    op.create_index(
        "idx_web_evt_inv_item", "web_event_team_inventory",
        ["event_id", "team_id", "shop_item_id"],
    )

    # -- Catalog seed (15 new power-ups) -----------------------------------
    items = sa.table(
        "web_boardgame_shop_items",
        sa.column("key", sa.String), sa.column("name", sa.String),
        sa.column("description", sa.Text), sa.column("icon_item_id", sa.Integer),
        sa.column("item_type", sa.String), sa.column("effect", sa.String),
        sa.column("effect_config", sa.Text), sa.column("cost_coins", sa.Integer),
        sa.column("type_cooldown_turns", sa.Integer), sa.column("sort", sa.Integer),
        sa.column("active", sa.Boolean),
    )
    op.bulk_insert(items, [
        {"key": "cache_of_runes", "name": "Cache of Runes",
         "description": "Draw 3 same-tier candidate tasks for your tile and pick one.",
         "icon_item_id": 12791, "item_type": "utility", "effect": "choose_task",
         "effect_config": '{"candidates": 3, "same_difficulty": true}',
         "cost_coins": 150, "type_cooldown_turns": 4, "sort": 100, "active": True},
        {"key": "binding_necklace", "name": "Binding Necklace",
         "description": "Draw 2 candidate tasks of differing difficulty and pick one.",
         "icon_item_id": 5521, "item_type": "utility", "effect": "choose_task",
         "effect_config": '{"candidates": 2, "distinct_difficulty": true}',
         "cost_coins": 100, "type_cooldown_turns": 3, "sort": 110, "active": True},
        {"key": "enchanted_die", "name": "Reroll",
         "description": "Undo your last move and roll again from where you started.",
         "icon_item_id": 13102, "item_type": "movement", "effect": "reroll_move",
         "effect_config": None,
         "cost_coins": 120, "type_cooldown_turns": 4, "sort": 120, "active": True},
        {"key": "mischievous_rat", "name": "Mischievous Rat",
         "description": "Steal a random item from a rival team.",
         "icon_item_id": 10092, "item_type": "offensive", "effect": "steal_item",
         "effect_config": None,
         "cost_coins": 180, "type_cooldown_turns": 5, "sort": 130, "active": True},
        {"key": "pirate_petes_parrot", "name": "Pirate Pete's Parrot",
         "description": "Squawk! Pilfer a random item from a rival team.",
         "icon_item_id": 12608, "item_type": "offensive", "effect": "steal_item",
         "effect_config": None,
         "cost_coins": 200, "type_cooldown_turns": 5, "sort": 140, "active": True},
        {"key": "rat_poison", "name": "Rat Poison",
         "description": "Ward off the next item-theft attempt against your team.",
         "icon_item_id": 187, "item_type": "defensive", "effect": "ward",
         "effect_config": '{"blocks": ["steal_item"]}',
         "cost_coins": 90, "type_cooldown_turns": 3, "sort": 150, "active": True},
        {"key": "ward_scroll", "name": "Protective Ward",
         "description": "Negate the next offensive effect used against your team.",
         "icon_item_id": 12817, "item_type": "defensive", "effect": "ward",
         "effect_config": '{"blocks": ["offensive"]}',
         "cost_coins": 140, "type_cooldown_turns": 4, "sort": 160, "active": True},
        {"key": "intricate_pouch", "name": "Intricate Pouch",
         "description": "Force a rival team to reroll their current task.",
         "icon_item_id": 5510, "item_type": "offensive", "effect": "reroll_opponent_task",
         "effect_config": None,
         "cost_coins": 130, "type_cooldown_turns": 4, "sort": 170, "active": True},
        {"key": "super_strength", "name": "Super Strength Potion",
         "description": "Add an extra die to your next roll.",
         "icon_item_id": 2440, "item_type": "movement", "effect": "extra_dice",
         "effect_config": '{"extra_dice": 1}',
         "cost_coins": 150, "type_cooldown_turns": 4, "sort": 180, "active": True},
        {"key": "wizards_mind_bomb", "name": "Wizard's Mind Bomb",
         "description": "Choose the exact value of your next roll.",
         "icon_item_id": 1907, "item_type": "movement", "effect": "choose_roll",
         "effect_config": None,
         "cost_coins": 220, "type_cooldown_turns": 6, "sort": 190, "active": True},
        {"key": "ghommals_penny", "name": "Ghommal's Lucky Penny",
         "description": "Reroll your current task for a fresh one of the same tier.",
         "icon_item_id": 25469, "item_type": "utility", "effect": "reroll_task",
         "effect_config": '{"difficulty_shift": 0}',
         "cost_coins": 80, "type_cooldown_turns": 3, "sort": 200, "active": True},
        {"key": "escape_crystal", "name": "Escape Crystal",
         "description": "Reroll your current task, drawing from one tier easier.",
         "icon_item_id": 13102, "item_type": "utility", "effect": "reroll_task",
         "effect_config": '{"difficulty_shift": -1}',
         "cost_coins": 130, "type_cooldown_turns": 4, "sort": 210, "active": True},
        {"key": "gloves_of_silence", "name": "Gloves of Silence",
         "description": "Your next roll tolls coins from every rival team you pass over.",
         "icon_item_id": 10075, "item_type": "economy", "effect": "coin_toll",
         "effect_config": '{"coins_per_team": 25}',
         "cost_coins": 160, "type_cooldown_turns": 5, "sort": 220, "active": True},
        {"key": "bandos_godsword", "name": "Bandos Godsword",
         "description": "Knock a rival team back 3 tiles.",
         "icon_item_id": 11804, "item_type": "offensive", "effect": "knockback",
         "effect_config": '{"tiles": 3}',
         "cost_coins": 260, "type_cooldown_turns": 6, "sort": 230, "active": True},
        {"key": "prayer_potion", "name": "Prayer Potion",
         "description": "Cleanse the negative effects afflicting your team.",
         "icon_item_id": 2434, "item_type": "defensive", "effect": "cleanse",
         "effect_config": None,
         "cost_coins": 110, "type_cooldown_turns": 4, "sort": 240, "active": True},
    ])


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM web_boardgame_shop_items WHERE `key` IN :keys")
        .bindparams(sa.bindparam("keys", value=list(_SEED_KEYS), expanding=True))
    )
    op.drop_index("idx_web_evt_inv_item", table_name="web_event_team_inventory")
    op.drop_column("web_event_board_positions", "pending_choice")
    op.drop_column("web_event_board_config", "shop_refreshed_turn")
    op.drop_column("web_event_board_config", "shop_refreshed_at")
    op.drop_column("web_event_shop_rotation", "per_team_cap")
    op.drop_column("web_event_shop_rotation", "stock_per_refresh")
    op.drop_column("web_event_shop_rotation", "enabled")
