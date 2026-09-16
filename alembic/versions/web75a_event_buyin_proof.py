"""Screenshot proof on prize-pot contributions (web75a).

``web_event_buyins.proof_url``: the optional screenshot backing one buy-in or
donation — the trade window, the "you have received" chat line. Mirrors
``web_event_completions.proof_url`` (same 255-char CDN URL, derived server-side
from an uploaded object key), so the pot ledger gains visual evidence without
inventing a second storage idiom.

Revision ID: web75a_event_buyin_proof
Revises: web74a_effort_visibility
"""
from alembic import op
import sqlalchemy as sa

revision = "web75a_event_buyin_proof"
down_revision = "web74a_effort_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_buyins",
        sa.Column("proof_url", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_event_buyins", "proof_url")
