"""drops: composite index (player_id, date_added) for the player-updater rebuild.

The background player updater (data/player_total_updater.py) rebuilds a player's
Redis cache from:

    SELECT ... FROM drops WHERE player_id = :pid AND hidden != true
    ORDER BY drops.date_added ASC

`drops` has single-column indexes on `player_id` and on `date_added`, but no
composite. For a player with a large drop history on a big, write-hot table the
optimizer either filesorts the player's rows or walks the `date_added` index —
either way the query blew past pymysql's read_timeout (30s), killing the
connection ("Lost connection to MySQL server during query (timed out)") and, on
the shared session, poisoning the next player in the batch. See the
droptracker-player-updates chronic-error investigation (2026-07-15).

A composite on (player_id, date_added) lets MariaDB seek straight to the
player's rows AND return them already ordered by date_added — no filesort, no
scan — so the rebuild returns in well under the timeout.

Applied online (ALGORITHM=INPLACE, LOCK=NONE) so it does not block the ~5/sec
intake writes to `drops` while building.

Revision ID: data01_drops_player_date_idx
Revises: web42a_event_team_auto_clan
"""
from alembic import op
import sqlalchemy as sa

revision = "data01_drops_player_date_idx"
down_revision = "web42a_event_team_auto_clan"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_drops_player_id_date_added"


def upgrade() -> None:
    conn = op.get_bind()
    existing = {ix["name"] for ix in sa.inspect(conn).get_indexes("drops")}
    if INDEX_NAME in existing:
        return
    # ALTER TABLE ... ADD INDEX form so ALGORITHM/LOCK options are accepted;
    # LOCK=NONE keeps concurrent inserts/reads flowing during the build.
    op.execute(
        f"ALTER TABLE drops ADD INDEX {INDEX_NAME} (player_id, date_added), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def downgrade() -> None:
    conn = op.get_bind()
    existing = {ix["name"] for ix in sa.inspect(conn).get_indexes("drops")}
    if INDEX_NAME not in existing:
        return
    op.execute(
        f"ALTER TABLE drops DROP INDEX {INDEX_NAME}, ALGORITHM=INPLACE, LOCK=NONE"
    )
