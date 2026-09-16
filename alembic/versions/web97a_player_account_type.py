"""Player OSRS game mode (web97a).

Adds the nullable ``players.account_type`` column behind Task 23. The RuneLite
plugin reports the account's game mode (varbit 1777) on each submission;
``data/submissions/common.apply_account_type()`` validates the wire string
against the seven-mode enum and stores it last-write-wins, so a de-ironed
account downgrades to ``normal`` on its next submission.

NULL means "never reported" — there is no backfill, and the Web API omits the
field entirely for those players rather than guessing ``normal``. The column
populates organically as players on a plugin build that sends the field submit.

Revision ID: web97a_player_account_type
Revises: web96a_chat_threads
"""
from alembic import op
import sqlalchemy as sa

revision = "web97a_player_account_type"
down_revision = "web96a_chat_threads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "players",
        sa.Column("account_type", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("players", "account_type")
