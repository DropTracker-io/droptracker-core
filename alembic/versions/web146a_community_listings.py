"""community_listings + community_listing_maintainers: /tools directory (web146a).

The web /tools page (tools and communities The DropTracker uses or points
players to) moves from a hard-coded list in the web repo to the database, so
staff can add, edit, hide and remove listings from the site and hand a listing
to its maintainer to keep up to date.

Seeds the four listings the hard-coded page carried, so the page looks the
same straight after the migration.

New tables only, so it is safe to apply live. Guarded, so a re-run is harmless.

Revision ID: web146a_community_listings
Revises: web145a_tester_update_keys
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "web146a_community_listings"
down_revision = "web145a_tester_update_keys"
branch_labels = None
depends_on = None


SEED = [
    {
        "slug": "wise-old-man",
        "name": "Wise Old Man",
        "section": "progress",
        "summary": (
            "Open-source progress tracker for Old School RuneScape. Tracks XP, boss kills "
            "and activity over time, and runs clan competitions."
        ),
        "fit_label": "Syncs rosters",
        "fit_note": (
            "Group rosters sync from Wise Old Man, and Skill and Boss of the Week results "
            "use its XP and kill-count gains."
        ),
        "tags": ["Progress tracking", "Competitions", "Open source"],
        "links": [
            {"label": "wiseoldman.net", "href": "https://wiseoldman.net"},
            {"label": "GitHub", "href": "https://github.com/wise-old-man/wise-old-man"},
        ],
        "accent": "#3b82f6",
        "sort_order": 10,
    },
    {
        "slug": "osrs-wiki",
        "name": "Old School RuneScape Wiki",
        "section": "foundations",
        "summary": (
            "The community-run wiki for the game. It also runs a real-time Grand Exchange "
            "price API built with RuneLite."
        ),
        "fit_label": "Prices and drop tables",
        "fit_note": "Item prices and boss drop tables on The DropTracker come from the wiki.",
        "tags": ["Reference", "Prices"],
        "links": [
            {"label": "oldschool.runescape.wiki", "href": "https://oldschool.runescape.wiki"},
            {"label": "Real-time prices", "href": "https://prices.runescape.wiki"},
        ],
        "accent": "#b8860b",
        "sort_order": 10,
    },
    {
        "slug": "runelite",
        "name": "RuneLite",
        "section": "foundations",
        "summary": (
            "The open-source third-party client for Old School RuneScape, with a hub of "
            "community plugins."
        ),
        "fit_label": "Runs our plugin",
        "fit_note": "Our plugin runs on RuneLite and is installed from the Plugin Hub.",
        "tags": ["Client", "Plugins", "Open source"],
        "links": [
            {"label": "runelite.net", "href": "https://runelite.net"},
            {"label": "Plugin Hub", "href": "https://runelite.net/plugin-hub"},
        ],
        "accent": "#d97706",
        "sort_order": 20,
    },
    {
        "slug": "osrs-clans",
        "name": "OSRS Clans",
        "section": "clans",
        "summary": (
            "A directory for finding and recruiting clans, with a list of community groups "
            "and clan utilities."
        ),
        "fit_label": None,
        "fit_note": None,
        "tags": ["Clan directory", "Recruiting"],
        "links": [
            {"label": "osrsclans.cc", "href": "https://osrsclans.cc"},
            {"label": "Communities", "href": "https://osrsclans.cc/communities"},
        ],
        "accent": "#16a34a",
        "sort_order": 10,
    },
]


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("community_listings"):
        return
    listings = op.create_table(
        "community_listings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("section", sa.String(24), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("fit_label", sa.String(40), nullable=True),
        sa.Column("fit_note", sa.String(400), nullable=True),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("links_json", sa.Text(), nullable=False),
        sa.Column("logo_url", sa.String(512), nullable=True),
        sa.Column("accent", sa.String(7), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_community_listings_slug"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"]),
    )
    op.create_index(
        "idx_community_listing_order", "community_listings", ["section", "sort_order", "id"]
    )

    op.create_table(
        "community_listing_maintainers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("listing_id", "user_id", name="uix_community_listing_maintainer"),
        sa.ForeignKeyConstraint(["listing_id"], ["community_listings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["added_by"], ["users.user_id"]),
    )
    op.create_index(
        "idx_community_maintainer_user", "community_listing_maintainers", ["user_id"]
    )

    op.bulk_insert(
        listings,
        [
            {
                "slug": row["slug"],
                "name": row["name"],
                "section": row["section"],
                "summary": row["summary"],
                "details": None,
                "fit_label": row["fit_label"],
                "fit_note": row["fit_note"],
                "tags_json": json.dumps(row["tags"]),
                "links_json": json.dumps(row["links"]),
                "logo_url": None,
                "accent": row["accent"],
                "sort_order": row["sort_order"],
                "visible": True,
            }
            for row in SEED
        ],
    )


def downgrade():
    op.drop_index("idx_community_maintainer_user", table_name="community_listing_maintainers")
    op.drop_table("community_listing_maintainers")
    op.drop_index("idx_community_listing_order", table_name="community_listings")
    op.drop_table("community_listings")
