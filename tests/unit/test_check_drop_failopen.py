"""Unit tests for osrs_api.semantic.SemanticAPI.check_drop fail-open behavior.

check_drop gates >1M drops against the wiki's `dropsline` data. It must reject
ONLY on a confident negative (the item has known drop sources and the stated
NPC isn't among them). Every inconclusive outcome — wiki unavailable, malformed
query, item not indexed, or an unexpected error — must fail OPEN (return True)
so a transient wiki problem never silently discards a legitimate high-value
drop. This regression guards the fix for that class of false rejection.
"""
import asyncio
import importlib.util
import os

# The test suite stubs the whole `osrs_api` package as a MagicMock (see
# tests/conftest.py), so import the real SemanticAPI straight from its source
# file. semantic.py only depends on the stdlib at import time.
_SEMANTIC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "osrs_api",
    "semantic.py",
)
_spec = importlib.util.spec_from_file_location("_real_semantic_for_test", _SEMANTIC_PATH)
_real_semantic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real_semantic)
SemanticAPI = _real_semantic.SemanticAPI


def _api(bucket_query_result=None, exc=None):
    api = SemanticAPI(client=object())

    async def fake_bucket_query(query):
        if exc is not None:
            raise exc
        return bucket_query_result

    api._bucket_query = fake_bucket_query
    return api


def _run(coro):
    return asyncio.run(coro)


def test_confident_match_returns_true():
    api = _api({"bucket": [{"page_name": "Zulrah"}]})
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True


def test_confident_negative_returns_false():
    # Item HAS known sources, but the stated NPC isn't one of them -> spoof.
    api = _api({"bucket": [{"page_name": "Chambers of Xeric"}]})
    assert _run(api.check_drop("Twisted bow", "Goblin")) is False


def test_subpage_reference_is_stripped_before_match():
    api = _api({"bucket": [{"page_name": "Zulrah#Uniques"}]})
    assert _run(api.check_drop("Serpentine visage", "Zulrah")) is True


def test_encounter_alias_matches_wiki_page():
    # Submitted encounter names accept their per-boss / variant wiki pages.
    cases = [
        ("Fire element staff crown", "Royal Titans", "Branda the Fire Queen"),
        ("Ice element staff crown", "Royal Titans", "Eldric the Ice King"),
        ("Granite hammer", "Dusk", "Grotesque Guardians"),
        ("Broken zombie axe", "Armoured zombie", "Armoured zombie (Zemouregal's Fort)"),
        ("Osmumten's fang", "Tombs of Amascut: Entry Mode", "Chest (Tombs of Amascut)"),
        ("Onyx", "Tekton (enraged)", "Tekton"),
        ("Tome of fire (empty)", "Reward cart (Wintertodt)", "Reward Cart"),
        ("Ring of 3rd age", "Clue Scroll (Master)", "The Mimic"),
        ("Ring of 3rd age", "Clue Scroll (Elite)", "The Mimic"),
    ]
    for item, npc, wiki_page in cases:
        api = _api({"bucket": [{"page_name": wiki_page}]})
        assert _run(api.check_drop(item, npc)) is True, (item, npc, wiki_page)


def test_alias_does_not_widen_spoof_surface():
    # An alias for one NPC must not make an unrelated NPC acceptable.
    api = _api({"bucket": [{"page_name": "Grotesque Guardians"}]})
    assert _run(api.check_drop("Granite hammer", "Zulrah")) is False


def test_activity_source_is_exempt():
    # Kingdom of Miscellania is an activity; dropsline can never confirm it.
    api = _api({"bucket": [{"page_name": "Coal rocks"}]})
    assert _run(api.check_drop("Coal", "Kingdom of Miscellania")) is True


def test_empty_dropsline_fails_open():
    # Successful query, but the item has no drop-source rows (new item / name
    # variant / wiki gap). Absence of data is not proof of a spoof.
    api = _api({"bucket": []})
    assert _run(api.check_drop("Some New Item", "Zulrah")) is True


def test_full_result_page_fails_open():
    """A result set AT the row limit may have been truncated by the Bucket API,
    so "the stated NPC isn't in this list" proves nothing. Inconclusive."""
    limit = SemanticAPI.DROPSLINE_ROW_LIMIT
    api = _api({"bucket": [{"page_name": f"Monster {i}"} for i in range(limit)]})
    assert _run(api.check_drop("Coins", "Tombs of Amascut")) is True


def test_partial_result_page_is_still_a_confident_negative():
    """Below the limit the wiki sent everything it had, so a miss is real."""
    limit = SemanticAPI.DROPSLINE_ROW_LIMIT
    api = _api({"bucket": [{"page_name": f"Monster {i}"} for i in range(limit - 1)]})
    assert _run(api.check_drop("Twisted bow", "Goblin")) is False


def test_dropsline_query_requests_an_explicit_row_limit():
    """Regression: a query with no .limit() silently gets the Bucket API's
    500-row default. "Coins" has 2,069 dropsline rows across 601 sources, so
    its real Tombs of Amascut source fell outside the slice and every coin
    drop over 1M from the raid was rejected as a spoof."""
    seen = []
    api = SemanticAPI(client=object())

    async def fake_bucket_query(query):
        seen.append(query)
        return {"bucket": [{"page_name": "Zulrah"}]}

    api._bucket_query = fake_bucket_query
    _run(api.check_drop("Tanzanite fang", "Zulrah"))
    assert f".limit({SemanticAPI.DROPSLINE_ROW_LIMIT})" in seen[0]


def _api_by_name(results_by_lookup_name):
    """SemanticAPI whose fake bucket query answers per queried item name.

    ``results_by_lookup_name`` maps the exact dropsline item_name being queried
    to that query's response dict. Also records the queries it saw."""
    api = SemanticAPI(client=object())
    api.seen_queries = []

    async def fake_bucket_query(query):
        api.seen_queries.append(query)
        for name, response in results_by_lookup_name.items():
            if api._bucket_quote(name) in query:
                return response
        raise AssertionError(f"unexpected dropsline query: {query}")

    api._bucket_query = fake_bucket_query
    return api


def test_uncharged_variant_gives_confident_negative():
    """Regression (2026-08-05): 'Tumeken's shadow from Dossier'. The charged
    name has NO dropsline rows (only the '(uncharged)' form drops, from the
    ToA chest), so the old code failed open for ANY stated source. The
    variant's source list is authoritative — the charged form drops nowhere."""
    api = _api_by_name({
        "Tumeken's shadow": {"bucket": []},
        "Tumeken's shadow (uncharged)": {"bucket": [{"page_name": "Chest (Tombs of Amascut)"}]},
    })
    assert _run(api.check_drop("Tumeken's shadow", "Dossier")) is False


def test_uncharged_variant_source_match_allows():
    # Same retry, but the stated NPC IS the variant's source (via alias):
    # a mis-named but genuinely ToA-sourced submission must not be rejected.
    api = _api_by_name({
        "Tumeken's shadow": {"bucket": []},
        "Tumeken's shadow (uncharged)": {"bucket": [{"page_name": "Chest (Tombs of Amascut)"}]},
    })
    assert _run(api.check_drop("Tumeken's shadow", "Tombs of Amascut")) is True


def test_variant_not_queried_when_primary_name_has_rows():
    api = _api_by_name({"Scythe of vitur (uncharged)": {"bucket": [{"page_name": "Monumental chest"}]}})
    assert _run(api.check_drop("Scythe of vitur (uncharged)", "Theatre of Blood")) is True
    assert len(api.seen_queries) == 1


def test_uncharged_name_gets_no_double_suffix():
    # A name already ending in "(uncharged)" with no rows anywhere: exactly one
    # query (no "... (uncharged) (uncharged)") and the not-indexed fail-open.
    api = _api_by_name({"Tumeken's shadow (uncharged)": {"bucket": []}})
    assert _run(api.check_drop("Tumeken's shadow (uncharged)", "Dossier")) is True
    assert len(api.seen_queries) == 1


def test_variant_also_unindexed_fails_open():
    api = _api_by_name({
        "Some New Item": {"bucket": []},
        "Some New Item (uncharged)": {"bucket": []},
    })
    assert _run(api.check_drop("Some New Item", "Zulrah")) is True


def test_wiki_error_on_variant_query_fails_open():
    api = _api_by_name({
        "Tumeken's shadow": {"bucket": []},
        "Tumeken's shadow (uncharged)": {},
    })
    assert _run(api.check_drop("Tumeken's shadow", "Dossier")) is True


def test_wiki_error_response_fails_open():
    # _bucket_query returns a dict WITHOUT a 'bucket' key on transport/API error.
    api = _api({})
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True


def test_unexpected_exception_fails_open():
    api = _api(exc=RuntimeError("wiki 503"))
    assert _run(api.check_drop("Tanzanite fang", "Zulrah")) is True
