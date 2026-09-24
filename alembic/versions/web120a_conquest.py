"""Conquest event kind (web120a).

A Risk-style territory event: a map of regions and tiles, where playing a
tile's tasks earns troops that claim, fortify or attack it. Eight new tables
(models in ``db/models/event_conquest.py``, rules in ``services/conquest.py``):

``web_conquest_maps``     one per event: settings JSON, background, clocks
``web_conquest_regions``  named tile groups with a control bonus
``web_conquest_tiles``    the territories, with their live owner + defense
``web_conquest_rules``    tile -> event task + troops per completion
``web_conquest_edges``    which tiles border each other
``web_conquest_holds``    ownership history (hold-time scoring source)
``web_conquest_battles``  every troop and what it did
``web_conquest_troops``   per (tile, team) troops earned + revoke debt

No FK points at ``web_event_teams`` on purpose (the model module explains the
lock ordering); event, tile and task FKs cascade on delete.

Also registers the ``conquest`` kind in ``web_event_types`` switched OFF and
staff-only, which hides it from everyone but superadmins (and any test group
added later) until the owner opens it up on /admin/event-types.

Only CREATEs new tables and inserts one registry row, so it is safe to apply
while events are running. Every step is guarded, so a re-run after a partial
failure picks up where it stopped.

Revision ID: web120a_conquest
Revises: web119a_staff_hosted_cvc
"""
from alembic import op
import sqlalchemy as sa

revision = "web120a_conquest"
down_revision = "web119a_staff_hosted_cvc"
branch_labels = None
depends_on = None

CONQUEST_DESCRIPTION = (
    "A territory war on a map of Gielinor, inspired by the board game Risk. "
    "Each territory is a boss or activity, and playing it earns your team "
    "troops that claim, defend or attack it, with the dice rolled for you. "
    "Hold whole regions for bonus points."
)


def _event_fk():
    return sa.ForeignKey("web_events.id", ondelete="CASCADE")


def _tile_fk():
    return sa.ForeignKey("web_conquest_tiles.id", ondelete="CASCADE")


def upgrade() -> None:
    bind = op.get_bind()
    # Fail fast instead of queueing behind a long metadata lock on web_events
    # (a CREATE TABLE with a foreign key briefly locks the parent's metadata).
    bind.execute(sa.text("SET SESSION lock_wait_timeout = 30"))
    existing = set(sa.inspect(bind).get_table_names())

    if "web_conquest_maps" not in existing:
        op.create_table(
            "web_conquest_maps",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("settings", sa.Text(), nullable=True),
            sa.Column("background_url", sa.String(255), nullable=True),
            sa.Column("bg_width", sa.Integer(), nullable=True),
            sa.Column("bg_height", sa.Integer(), nullable=True),
            sa.Column("preset", sa.String(40), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("seeded_at", sa.DateTime(), nullable=True),
            sa.Column("settled_at", sa.DateTime(), nullable=True),
            sa.Column("summary_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("uq_web_conquest_map_event", "web_conquest_maps",
                        ["event_id"], unique=True)

    if "web_conquest_regions" not in existing:
        op.create_table(
            "web_conquest_regions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("color", sa.String(7), nullable=True),
            sa.Column("bonus", sa.Float(), nullable=False, server_default="0"),
            sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("label_x", sa.Float(), nullable=True),
            sa.Column("label_y", sa.Float(), nullable=True),
            sa.Column("owner_team_id", sa.Integer(), nullable=True),
            sa.Column("owner_since", sa.DateTime(), nullable=True),
        )
        op.create_index("idx_web_conquest_region_event", "web_conquest_regions",
                        ["event_id"])

    if "web_conquest_tiles" not in existing:
        op.create_table(
            "web_conquest_tiles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("region_id", sa.Integer(),
                      sa.ForeignKey("web_conquest_regions.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("idx", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("label", sa.String(80), nullable=False),
            sa.Column("x", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("y", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("kind", sa.String(16), nullable=False, server_default="normal"),
            sa.Column("value", sa.Float(), nullable=False, server_default="1"),
            sa.Column("icon_npc_id", sa.Integer(), nullable=True),
            sa.Column("icon_item_id", sa.Integer(), nullable=True),
            sa.Column("owner_team_id", sa.Integer(), nullable=True),
            sa.Column("defense", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("owner_since", sa.DateTime(), nullable=True),
            sa.Column("captures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_battle_at", sa.DateTime(), nullable=True),
        )
        op.create_index("idx_web_conquest_tile_event", "web_conquest_tiles",
                        ["event_id", "idx"])

    if "web_conquest_rules" not in existing:
        op.create_table(
            "web_conquest_rules",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("tile_id", sa.Integer(), _tile_fk(), nullable=False),
            sa.Column("task_id", sa.Integer(),
                      sa.ForeignKey("web_event_tasks.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("troops", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("uq_web_conquest_rule_task", "web_conquest_rules",
                        ["task_id"], unique=True)
        op.create_index("idx_web_conquest_rule_tile", "web_conquest_rules",
                        ["tile_id"])

    if "web_conquest_edges" not in existing:
        op.create_table(
            "web_conquest_edges",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("tile_a_id", sa.Integer(), _tile_fk(), nullable=False),
            sa.Column("tile_b_id", sa.Integer(), _tile_fk(), nullable=False),
        )
        op.create_index("uq_web_conquest_edge", "web_conquest_edges",
                        ["event_id", "tile_a_id", "tile_b_id"], unique=True)

    if "web_conquest_holds" not in existing:
        op.create_table(
            "web_conquest_holds",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("tile_id", sa.Integer(), _tile_fk(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
        )
        op.create_index("idx_web_conquest_hold_event", "web_conquest_holds",
                        ["event_id", "tile_id"])

    if "web_conquest_battles" not in existing:
        op.create_table(
            "web_conquest_battles",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("tile_id", sa.Integer(), _tile_fk(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=True),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("owner_before", sa.Integer(), nullable=True),
            sa.Column("owner_after", sa.Integer(), nullable=True),
            sa.Column("defense_before", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("defense_after", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attack_dice", sa.String(16), nullable=True),
            sa.Column("defense_dice", sa.String(16), nullable=True),
            sa.Column("player_id", sa.Integer(), nullable=True),
            sa.Column("completion_id", sa.BigInteger(), nullable=True),
            sa.Column("task_id", sa.Integer(), nullable=True),
            sa.Column("source", sa.String(16), nullable=False, server_default="troop"),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("idx_web_conquest_battle_event", "web_conquest_battles",
                        ["event_id", "id"])
        op.create_index("idx_web_conquest_battle_tile", "web_conquest_battles",
                        ["tile_id", "id"])

    if "web_conquest_troops" not in existing:
        op.create_table(
            "web_conquest_troops",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(), _event_fk(), nullable=False),
            sa.Column("tile_id", sa.Integer(), _tile_fk(), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("earned", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("debt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("uq_web_conquest_troops", "web_conquest_troops",
                        ["tile_id", "team_id"], unique=True)
        op.create_index("idx_web_conquest_troops_event", "web_conquest_troops",
                        ["event_id", "team_id"])

    registered = bind.execute(
        sa.text("SELECT 1 FROM web_event_types WHERE `key` = 'conquest'")
    ).first()
    if registered is None:
        types = sa.table(
            "web_event_types",
            sa.column("key", sa.String),
            sa.column("label", sa.String),
            sa.column("description", sa.Text),
            sa.column("enabled", sa.Boolean),
            sa.column("admin_only", sa.Boolean),
            sa.column("sort", sa.Integer),
        )
        op.bulk_insert(types, [{
            "key": "conquest",
            "label": "Conquest",
            "description": CONQUEST_DESCRIPTION,
            # Off + staff-only: hidden from every group (bar test groups)
            # until the owner opens it up.
            "enabled": False,
            "admin_only": True,
            "sort": 8,
        }])


def downgrade() -> None:
    op.execute("DELETE FROM web_event_type_test_groups WHERE type_key = 'conquest'")
    op.execute("DELETE FROM web_event_types WHERE `key` = 'conquest'")
    for table in ("web_conquest_troops", "web_conquest_battles", "web_conquest_holds",
                  "web_conquest_edges", "web_conquest_rules", "web_conquest_tiles",
                  "web_conquest_regions", "web_conquest_maps"):
        op.drop_table(table)
