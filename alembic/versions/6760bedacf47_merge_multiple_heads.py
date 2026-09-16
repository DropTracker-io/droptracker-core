"""merge multiple heads

Revision ID: 6760bedacf47
Revises: 0fce499e9829
Create Date: 2026-04-23 13:44:45.829741

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6760bedacf47'
down_revision: Union[str, None] = '0fce499e9829'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
