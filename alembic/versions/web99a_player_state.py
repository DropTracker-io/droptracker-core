"""Plugin account-state sync, PB loadouts, manifest and component layouts (web99a).

Eight tables behind the plugin's state-sync update. The `feat/state-sync` work
shipped the models but no revision — `alembic/versions/` is gitignored, so a
migration authored on a branch never travels with it. This is that migration,
written from the model definitions in `db/models/`.

The five `player_*` tables are **state, not events**: one row per player per
item / quest / diary tier, rewritten in place by each sync rather than appended
to. That is what makes the sync endpoint safe to retry, and it is why they carry
natural composite primary keys instead of surrogate ids — the upsert has to have
something to conflict on.

`personal_best_loadouts` is deliberately not columns on `personal_best`: the data
is optional (older clients send none), and `personal_best` is large and hot
enough that an additive feature should not cost it an ALTER.

`plugin_manifest_sections` is server-controlled reference data the plugin reads
at startup, stored as independent rows so each section can be regenerated on its
own cadence.

`group_component_layouts` is parallel to `group_embeds`, not a replacement — a
group either has an active row for a notification type and sends Components V2,
or it does not and the embed path runs unchanged.

Sizing note: `player_clog_items` holds up to ~1,500 rows per player who opens
their collection log. Every access path in `web_api/routes/player_state.py` is
by primary key, so no secondary indexes are added here.

Revision ID: web99a_player_state
Revises: web98a_group_config_key_index
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "web99a_player_state"
down_revision = "web98a_group_config_key_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_state",
        sa.Column("player_id", sa.Integer(), nullable=False),
        # Raw IRONMAN varbit rather than a decoded label, so a new account type
        # does not need a migration to be storable.
        sa.Column("account_type", sa.SmallInteger(), nullable=True),
        sa.Column("combat_level", sa.SmallInteger(), nullable=True),
        sa.Column("clog_slots", sa.Integer(), nullable=True),
        sa.Column("clog_slots_total", sa.Integer(), nullable=True),
        sa.Column("manifest_version", sa.String(32), nullable=True),
        sa.Column("last_sync_source", sa.String(32), nullable=True),
        sa.Column("model_fingerprint", sa.String(32), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("player_id"),
    )

    op.create_table(
        "player_clog_items",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        # When *we* first recorded the item, not when the player obtained it —
        # a full scrape backfills items unlocked years ago.
        sa.Column("first_seen_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("player_id", "item_id"),
    )

    op.create_table(
        "player_ca_varps",
        sa.Column("player_id", sa.Integer(), nullable=False),
        # JSON object of varp id -> raw bits, stored undecoded on purpose.
        sa.Column("varps", sa.Text(), nullable=False),
        sa.Column("tasks_completed", sa.Integer(), nullable=True),
        sa.Column("completed_tasks", sa.Text(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("player_id"),
    )

    op.create_table(
        "player_quest_states",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("quest_id", sa.Integer(), nullable=False),
        # 0 not started, 1 in progress, 2 finished.
        sa.Column("state", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("player_id", "quest_id"),
    )

    op.create_table(
        "player_diary_tiers",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("area_id", sa.Integer(), nullable=False),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("player_id", "area_id", "tier"),
    )

    op.create_table(
        "personal_best_loadouts",
        sa.Column("pb_id", sa.Integer(), nullable=False),
        # JSON arrays of {"slot", "item_id", "quantity"}. NULL means the client
        # sent nothing; an empty array would wrongly say "wearing nothing".
        sa.Column("equipment", sa.Text(), nullable=True),
        sa.Column("inventory", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["pb_id"], ["personal_best.id"]),
        sa.PrimaryKeyConstraint("pb_id"),
    )

    op.create_table(
        "plugin_manifest_sections",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "group_component_layouts",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("notification_type", sa.String(32), nullable=False),
        # LONGTEXT: a rich layout with several long text blocks passes TEXT's
        # 64KB ceiling more easily than it looks.
        sa.Column("layout", mysql.LONGTEXT(), nullable=False),
        # False means "authored but not live".
        sa.Column("active", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.PrimaryKeyConstraint("group_id", "notification_type"),
    )


def downgrade() -> None:
    op.drop_table("group_component_layouts")
    op.drop_table("plugin_manifest_sections")
    op.drop_table("personal_best_loadouts")
    op.drop_table("player_diary_tiers")
    op.drop_table("player_quest_states")
    op.drop_table("player_ca_varps")
    op.drop_table("player_clog_items")
    op.drop_table("player_state")
