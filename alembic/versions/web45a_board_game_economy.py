"""Board-game economy (P2): shop catalog, rotation, inventory, cooldowns,
effects — the coin-spend half of the dice-board mode.

- ``web_boardgame_shop_items``   — site-wide curated power-up catalog
                                    (superadmin; PremiumFeature pattern).
- ``web_event_shop_rotation``    — per-event stocking/pricing/turn windows
                                    (no rows = sell the whole active catalog).
- ``web_event_team_inventory``   — team-held copies (FeatureActivation).
- ``web_event_team_cooldowns``   — last turn each item TYPE was used.
- ``web_event_effects``          — live boosts/roadblocks/freezes/shields.

Seeds the starter catalog from the legacy GielinorRace design: the P2
self-targeted set active, the P3 interference set present but inactive.

Revision ID: web45a_board_game_economy
Revises: web44a_board_game_core
"""
from alembic import op
import sqlalchemy as sa

revision = "web45a_board_game_economy"
down_revision = "web44a_board_game_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_boardgame_shop_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon_item_id", sa.Integer(), nullable=True),
        sa.Column("item_type", sa.String(16), nullable=False),
        sa.Column("effect", sa.String(24), nullable=False),
        sa.Column("effect_config", sa.Text(), nullable=True),
        sa.Column("cost_coins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("type_cooldown_turns", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("uq_web_bg_shop_key", "web_boardgame_shop_items", ["key"], unique=True)

    op.create_table(
        "web_event_shop_rotation",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column(
            "shop_item_id", sa.Integer(),
            sa.ForeignKey("web_boardgame_shop_items.id"), nullable=False,
        ),
        sa.Column("price_override", sa.Integer(), nullable=True),
        sa.Column("available_from_turn", sa.Integer(), nullable=True),
        sa.Column("available_until_turn", sa.Integer(), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_web_evt_shop_item", "web_event_shop_rotation",
        ["event_id", "shop_item_id"], unique=True,
    )

    op.create_table(
        "web_event_team_inventory",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=False),
        sa.Column(
            "shop_item_id", sa.Integer(),
            sa.ForeignKey("web_boardgame_shop_items.id"), nullable=False,
        ),
        sa.Column("price_paid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acquired_turn", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="owned"),
        sa.Column("used_turn", sa.Integer(), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "used_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True
        ),
        sa.Column("used_on", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_web_evt_inv_team", "web_event_team_inventory",
        ["event_id", "team_id", "status"],
    )

    op.create_table(
        "web_event_team_cooldowns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=False),
        sa.Column("item_type", sa.String(16), nullable=False),
        sa.Column("last_used_turn", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "uq_web_evt_cooldown", "web_event_team_cooldowns",
        ["team_id", "item_type"], unique=True,
    )

    op.create_table(
        "web_event_effects",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column(
            "source_team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=False
        ),
        sa.Column(
            "target_team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=True
        ),
        sa.Column("target_tile_idx", sa.Integer(), nullable=True),
        sa.Column("effect_type", sa.String(24), nullable=False),
        sa.Column("effect_config", sa.Text(), nullable=True),
        sa.Column("expires_turn", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "inventory_id", sa.Integer(),
            sa.ForeignKey("web_event_team_inventory.id"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_web_evt_effects_event", "web_event_effects", ["event_id", "status"])

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
        # -- P2: self-targeted set (active) --------------------------------
        {"key": "skip_token", "name": "Skip token",
         "description": "Instantly complete your current task (no coin reward) and move on.",
         "icon_item_id": 775,  # Lockpick
         "item_type": "utility", "effect": "skip_task", "effect_config": None,
         "cost_coins": 120, "type_cooldown_turns": 4, "sort": 10, "active": True},
        {"key": "reroll_scroll", "name": "Reroll scroll",
         "description": "Swap your current task for a fresh one from the same tile.",
         "icon_item_id": 9721,  # Scroll-ish
         "item_type": "utility", "effect": "reroll_task", "effect_config": None,
         "cost_coins": 60, "type_cooldown_turns": 2, "sort": 20, "active": True},
        {"key": "coin_chest", "name": "Coin chest",
         "description": "Double the coins from your next completed task.",
         "icon_item_id": 995,  # Coins
         "item_type": "economy", "effect": "boost_coins",
         "effect_config": '{"multiplier": 2}',
         "cost_coins": 80, "type_cooldown_turns": 3, "sort": 30, "active": True},
        # -- P3: interference set (present, inactive until handlers land) --
        {"key": "teleport_tablet", "name": "Teleport tablet",
         "description": "Jump 1-6 tiles forward without completing a task.",
         "icon_item_id": 8007,  # Varrock teleport
         "item_type": "movement", "effect": "advance",
         "effect_config": '{"dice_sides": 6}',
         "cost_coins": 150, "type_cooldown_turns": 5, "sort": 40, "active": False},
        {"key": "dinhs_bulwark", "name": "Dinh's bulwark",
         "description": "Place a roadblock on a tile — the next team to pass loses a turn.",
         "icon_item_id": 21015,
         "item_type": "defensive", "effect": "roadblock",
         "effect_config": '{"stall_turns": 1}',
         "cost_coins": 200, "type_cooldown_turns": 6, "sort": 50, "active": False},
        {"key": "ice_barrage", "name": "Ice barrage",
         "description": "Freeze another team — they cannot roll for 2 of their turns.",
         "icon_item_id": 6905,  # Water battlestaff-ish placeholder icon
         "item_type": "offensive", "effect": "freeze_opponent",
         "effect_config": '{"turns": 2}',
         "cost_coins": 250, "type_cooldown_turns": 6, "sort": 60, "active": False},
        {"key": "spirit_shield", "name": "Spirit shield",
         "description": "Blocks the next offensive item used against your team.",
         "icon_item_id": 12829,
         "item_type": "defensive", "effect": "shield", "effect_config": None,
         "cost_coins": 180, "type_cooldown_turns": 5, "sort": 70, "active": False},
    ])


def downgrade() -> None:
    op.drop_index("idx_web_evt_effects_event", table_name="web_event_effects")
    op.drop_table("web_event_effects")
    op.drop_index("uq_web_evt_cooldown", table_name="web_event_team_cooldowns")
    op.drop_table("web_event_team_cooldowns")
    op.drop_index("idx_web_evt_inv_team", table_name="web_event_team_inventory")
    op.drop_table("web_event_team_inventory")
    op.drop_index("uq_web_evt_shop_item", table_name="web_event_shop_rotation")
    op.drop_table("web_event_shop_rotation")
    op.drop_index("uq_web_bg_shop_key", table_name="web_boardgame_shop_items")
    op.drop_table("web_boardgame_shop_items")
