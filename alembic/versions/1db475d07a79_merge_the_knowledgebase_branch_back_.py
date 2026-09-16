"""Merge the knowledgebase branch back into the web line.

``kb01a_knowledgebase_tables`` was authored off ``web67a_ticket_inactivity``
while that was the head, and the ``web*`` line kept moving (through web75a)
without ever rejoining it. Two heads meant ``alembic upgrade head`` failed with
"Multiple head revisions are present" and every deploy had to name a revision
explicitly — easy to get wrong, and it silently left the other branch behind.

No DDL: a merge revision only collapses the graph (and ``alembic_version`` back
to a single row). Both parents were already applied in production.

Revision ID: 1db475d07a79
Revises: kb01a_knowledgebase_tables, web75a_event_buyin_proof
Create Date: 2026-07-29 16:33:12.984394

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1db475d07a79'
down_revision: Union[str, None] = ('kb01a_knowledgebase_tables', 'web75a_event_buyin_proof')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
