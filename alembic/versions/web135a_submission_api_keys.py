"""Submission API (suggestion #198): submit-capable keys + the submission log.

A clan's own Discord bot can push staff-approved submissions into DropTracker
through ``POST /v2/submissions``. Two key flags gate it:

* ``can_submit`` — the key may submit at all. Off for every existing key, so
  no read-only key gains write access by this migration.
* ``submit_preapproved`` — the group already reviewed what this key sends, so
  its submissions skip that group's manual-submission review and the
  ``confirm_non_api`` hold on events the group hosts or takes part in.

``api_submissions`` is one row per call: the audit trail (which key credited
what), the idempotency record (``external_id`` is unique per key, so a bot's
retry never double-credits), and the "already tracked by the plugin" ledger
the count-aware duplicate check reads.

Revision ID: web135a_submission_api_keys
Revises: web134a_death_activity_switches
"""
import sqlalchemy as sa
from alembic import op

revision = "web135a_submission_api_keys"
down_revision = "web134a_death_activity_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column(
        "can_submit", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("api_keys", sa.Column(
        "submit_preapproved", sa.Boolean(), nullable=False, server_default="0"))

    op.create_table(
        "api_submissions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("api_key_id", sa.BigInteger(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("submission_type", sa.String(24), nullable=False),
        sa.Column("item_name", sa.String(120), nullable=True),
        # processing | accepted | rejected | already_tracked | error
        sa.Column("status", sa.String(16), nullable=False, server_default="processing"),
        sa.Column("message", sa.String(255), nullable=True),
        sa.Column("submission_guid", sa.String(64), nullable=True),
        sa.Column("matched_drop_id", sa.Integer(), nullable=True),
        sa.Column("preapproved", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("image_url", sa.String(255), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("obtained_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_api_submission_external", "api_submissions",
                    ["api_key_id", "external_id"], unique=True)
    op.create_index("idx_api_submission_player_item", "api_submissions",
                    ["player_id", "item_name", "created_at"])
    # The group-scoped listing (GET /v2/submissions) pages by id.
    op.create_index("idx_api_submission_group", "api_submissions",
                    ["group_id", "id"])
    # One plugin drop can stand in for one API submission at most: two
    # concurrent calls for the same item cannot both be matched to it.
    op.create_index("uq_api_submission_matched_drop", "api_submissions",
                    ["matched_drop_id"], unique=True)


def downgrade() -> None:
    op.drop_table("api_submissions")
    op.drop_column("api_keys", "submit_preapproved")
    op.drop_column("api_keys", "can_submit")
