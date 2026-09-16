"""Index players by a normalized display name (web100a).

``db.ops.resolve_player_for_display`` falls back to OSRS display equivalence
when a submitted RSN matches no row exactly: the plugin sends the name as the
game spells it (``Beast_Owned``, ``X-tra``) while WOM, our identity source,
folds ``-`` and ``_`` to spaces. That fallback applied ``LOWER(REPLACE(REPLACE(
TRIM(player_name) ...)))`` to the **column**, which no index can serve, so it
degraded to a full scan of all ~22k rows (``type=index``, ~15ms).

It is not a rare path. It is reached on every name that resolves to nothing --
which is every unregistered participant in a raid roster -- and the point
awarder re-resolves the whole participant list once per group the receiver
belongs to. Measured on production 2026-08-29: **64% of the webhook consumer's
wall-clock** sat in that one query, and 93 of 99 sampled in-flight ``players``
queries server-wide were it. ``Handler_read_rnd_next`` ran at ~902k rows/sec,
which the profile accounts for almost exactly.

This adds the normalization as a generated column and indexes it, turning the
lookup into a ref seek (~0.29ms, 54x). Two notes on the expression:

* It mirrors ``utils.format.normalize_player_display_equivalence`` **exactly**,
  including the whitespace collapse the old SQL lacked. Verified against all
  22,088 live names: zero disagreements. The old SQL also trimmed *before*
  replacing, so a leading ``-`` became an untrimmed space -- six real players
  (``-NoDashes``, ``the  worst``, ``I         q``, ...) could never be found
  through this branch at all. They can now.
* VIRTUAL, so no table rebuild and no per-row storage; only the index is
  materialised.

**lock_wait_timeout here is 75s, not web98a's 5s — on this table 5s can never
win.** The api's pooled sessions rotate 30-60s open transactions against
``players`` continuously (measured 2026-08-29: at any instant several idle-in-
transaction holders, oldest ~40-64s), so a 5s-bounded exclusive MDL request
always times out — it failed four straight attempts. MariaDB queues MDL
requests fairly: once ours is pending, NEW readers queue behind it and we only
have to outwait the CURRENT holders, so 75s is enough in practice and also the
worst case for how long player lookups can stall (they queue, then either the
DDL runs in well under a second on 22k rows or the request aborts and reads
resume). The plugin retries through such a window by design.

The column add and the index build are separate statements: MariaDB will not
add a virtual column and an index over it in one INPLACE ALTER.

Revision ID: web100a_player_name_norm_index
Revises: f852c2a14f4e
"""
from alembic import op

revision = "web100a_player_name_norm_index"
down_revision = "f852c2a14f4e"
branch_labels = None
depends_on = None

_EXPR = (
    "LOWER(TRIM(REGEXP_REPLACE("
    "REPLACE(REPLACE(player_name, '_', ' '), '-', ' '),"
    " '[[:space:]]+', ' ')))"
)


def upgrade() -> None:
    # Long enough to outwait the api pools' rotating trx, still bounded; see
    # the module docstring.
    op.execute("SET SESSION lock_wait_timeout = 75")
    op.execute(
        "ALTER TABLE players ADD COLUMN player_name_norm "
        "VARCHAR(20) COLLATE utf8mb4_general_ci "
        f"AS ({_EXPR}) VIRTUAL"
    )
    op.execute(
        "ALTER TABLE players ADD INDEX ix_players_player_name_norm (player_name_norm)"
    )


def downgrade() -> None:
    op.execute("SET SESSION lock_wait_timeout = 75")
    op.execute("ALTER TABLE players DROP INDEX ix_players_player_name_norm")
    op.execute("ALTER TABLE players DROP COLUMN player_name_norm")
