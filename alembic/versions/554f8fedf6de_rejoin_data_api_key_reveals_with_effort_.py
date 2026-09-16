"""rejoin data-api key reveals with effort completions

Revision ID: 554f8fedf6de
Revises: dapi4_key_reveals, web104a_effort_completions
Create Date: 2026-08-28 11:10:56.139695

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '554f8fedf6de'
down_revision: Union[str, None] = ('dapi4_key_reveals', 'web104a_effort_completions')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
