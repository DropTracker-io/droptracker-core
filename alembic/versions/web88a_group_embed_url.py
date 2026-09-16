"""group_embeds.url — clickable embed titles.

Discord renders an embed *title* as plain text: a markdown link written into
`title` shows its raw brackets and URL. The only way to make a title clickable
is the embed's own `url` field, which had no column here, so the sole way a
group could get a linked title was to leave the literal {npc_name}/{item_name}
token in it and let utils.format.replace_placeholders auto-link the wiki page.

NULL preserves exactly that auto-link behaviour; a value overrides it and may
carry placeholders ({npc_id}, {item_id}, …), resolved at send time.

Revision ID: web88a_group_embed_url
Revises: web87a_developer_role
"""
from alembic import op
import sqlalchemy as sa

revision = "web88a_group_embed_url"
down_revision = "web87a_developer_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_embeds",
        sa.Column("url", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("group_embeds", "url")
