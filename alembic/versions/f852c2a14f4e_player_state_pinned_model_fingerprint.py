"""player_state pinned_model_fingerprint

Revision ID: f852c2a14f4e
Revises: 554f8fedf6de
Create Date: 2026-08-28 13:54:53.522286

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f852c2a14f4e'
down_revision: Union[str, None] = '554f8fedf6de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "player_state",
        sa.Column("pinned_model_fingerprint", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("player_state", "pinned_model_fingerprint")
