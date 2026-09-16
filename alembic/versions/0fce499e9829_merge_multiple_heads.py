"""merge multiple heads

Revision ID: 0fce499e9829
Revises: 4b1f2a7c9d11, 7b6e4af4b2c1
Create Date: 2026-04-23 13:43:41.701091

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0fce499e9829'
down_revision: Union[str, None] = ('4b1f2a7c9d11', '7b6e4af4b2c1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
