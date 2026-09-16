"""Ticket archive: transcript storage + close metadata.

Adds ticket_messages (mirrored Discord messages, written live by the webhook
bot and re-synced from channel history at close time) and extends tickets with
subject / date_closed / closed_by so the website can list and display archived
tickets after the Discord channel is deleted.

NOTE: run `alembic upgrade head` at deploy time, then restart
droptracker-webhooks (mirroring) and droptracker-webapi (routes).
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "web21a_ticket_archive"
down_revision = "web20a_channel_key_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("subject", sa.String(255), nullable=True))
    op.add_column("tickets", sa.Column("date_closed", sa.DateTime(), nullable=True))
    op.add_column(
        "tickets",
        sa.Column("closed_by", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
    )

    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "ticket_id",
            sa.Integer(),
            sa.ForeignKey("tickets.ticket_id"),
            nullable=False,
        ),
        sa.Column("discord_message_id", sa.String(32), nullable=True, unique=True),
        sa.Column(
            "author_user_id",
            sa.Integer(),
            sa.ForeignKey("users.user_id"),
            nullable=True,
        ),
        sa.Column("author_discord_id", sa.String(32), nullable=False),
        sa.Column("author_name", sa.String(100), nullable=False),
        sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("kind", sa.String(16), nullable=False, server_default="message"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("attachments_json", sa.Text(), nullable=True),
        sa.Column("date_sent", sa.DateTime(), nullable=False),
        sa.Column("date_edited", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_ticket_messages_ticket", "ticket_messages", ["ticket_id", "date_sent"]
    )
    op.create_index("idx_ticket_messages_author", "ticket_messages", ["author_user_id"])


def downgrade() -> None:
    op.drop_table("ticket_messages")
    op.drop_column("tickets", "closed_by")
    op.drop_column("tickets", "date_closed")
    op.drop_column("tickets", "subject")
