"""merge seasonal models with existing history

Revision ID: a9d60bddb823
Revises: 6760bedacf47, a3e17db90009
Create Date: 2026-04-23 14:04:41.689695

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9d60bddb823'
down_revision: Union[str, None] = ('6760bedacf47', 'a3e17db90009')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
