"""Docs full-text search (web_api/docs_search.py) — pure ranking over pages."""
from web_api.docs_search import heading_slug, query_terms, search_docs, split_sections

PAGES = [
    {
        "slug": "events-create",
        "title": "Running an event",
        "description": None,
        "category": "Events",
        "content": (
            "# Running an event\n\nEvery group can run events.\n\n"
            "## Joining and rules\n\n| Mode | How |\n|---|---|\n| Pick your team | Players choose. |\n\n"
            "## Prize pot\n\nThe **Prize Pot** tab tracks GP buy-ins and donations.\n\n"
            "```\n## not a heading\n```\n"
        ),
    },
    {
        "slug": "events-board",
        "title": "Board game events",
        "description": "Race across a tile board",
        "category": "Events",
        "content": (
            "# Board game events\n\nRoll the dice.\n\n"
            "## Power-up glossary\n\n| **Extra dice** | Add a die. |\n| **Ice barrage** | Freeze a team. |\n"
        ),
    },
    {
        "slug": "faq",
        "title": "FAQ",
        "description": None,
        "category": "Reference",
        "content": "# FAQ\n\n## My event progress isn't counting.\n\nTurn on [Use API Connections](/docs/runelite-plugin).",
    },
]


class TestHeadingSlug:
    def test_matches_web_rules(self):
        # Mirror of headingSlug() in web apps/web/lib/docs.ts.
        assert heading_slug("Joining and rules") == "joining-and-rules"
        assert heading_slug("Chutes & Ladders") == "chutes-ladders"
        assert heading_slug("My event progress isn't counting.") == "my-event-progress-isnt-counting"
        assert heading_slug("**Bold** [link](/x) `code`") == "bold-link-code"
        assert heading_slug("  Spaced   out  ") == "spaced-out"


class TestSections:
    def test_split_ignores_title_and_fenced_headings(self):
        secs = split_sections(PAGES[0]["content"])
        assert [s.heading for s in secs] == [None, "Joining and rules", "Prize pot"]
        assert "not a heading" in secs[-1].body

    def test_markdown_is_stripped_from_bodies(self):
        secs = split_sections(PAGES[2]["content"])
        assert secs[-1].body == "Turn on Use API Connections."

    def test_table_rules_dropped(self):
        body = split_sections(PAGES[0]["content"])[1].body
        assert "---" not in body and "|" not in body


class TestQueryTerms:
    def test_drops_stop_words(self):
        assert query_terms("How do I join an event?") == ["join", "event"]

    def test_only_stop_words_falls_back(self):
        assert query_terms("how to") == ["how", "to"]

    def test_empty(self):
        assert query_terms("  ?! ") == []


class TestSearch:
    def test_finds_body_text_and_points_at_section(self):
        hits = search_docs(PAGES, "prize pot")
        assert hits[0]["slug"] == "events-create"
        assert hits[0]["section"] == "Prize pot"
        assert hits[0]["anchor"] == "prize-pot"
        assert "buy-ins" in hits[0]["snippet"]

    def test_word_prefix_not_substring(self):
        # "ice" must not match "dice"; "barr" is a prefix of "barrage".
        assert [h["slug"] for h in search_docs(PAGES, "ice barr")] == ["events-board"]
        dice_only = [{"slug": "d", "title": "Dice", "content": "Roll the dice."}]
        assert search_docs(dice_only, "ice") == []

    def test_phrase_across_punctuation(self):
        hits = search_docs(PAGES, "buy in")
        assert hits[0]["anchor"] == "prize-pot"

    def test_title_outranks_body(self):
        hits = search_docs(PAGES, "board")
        assert hits[0]["slug"] == "events-board"

    def test_partial_match_fallback(self):
        # No page has all three terms; pages with half of them still come back.
        hits = search_docs(PAGES, "api connections xyzzy")
        assert hits and hits[0]["slug"] == "faq"
        assert hits[0]["anchor"] == "my-event-progress-isnt-counting"

    def test_intro_hit_has_no_anchor(self):
        hits = search_docs(PAGES, "every group")
        assert hits[0]["slug"] == "events-create"
        assert hits[0]["anchor"] is None

    def test_no_match(self):
        assert search_docs(PAGES, "zzzz") == []
        assert search_docs(PAGES, "") == []

    def test_limit(self):
        assert len(search_docs(PAGES, "e", limit=1)) <= 1
        assert len(search_docs(PAGES, "events", limit=1)) == 1
