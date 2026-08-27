"""Turning the game cache's collection log structure into the manifest.

The structure used to be scraped from the wiki, and the tests here used to cover
name-to-id resolution — a guess that cost real slots whenever it picked the
wrong version of an item. The cache states the ids outright, so what is left to
get wrong is the shaping: a payload whose names fall out of step with its ids
mislabels every slot after the gap, and an audit that cannot tell the plugin's
name-derived unlock ids from a genuine game update buries the one signal it
exists to produce.
"""
from scripts.sync_collection_log import (
    manifest_payload,
    name_derived_ids,
    slot_ids,
    slot_names,
)


def extract(*pages):
    """An extractor output holding one tab of the given pages."""
    return {"tabs": [{"name": "Bosses", "struct": 471, "pages": list(pages)}]}


def page(name, items, names=None, **extra):
    return {"name": name, "items": items, "names": names or [], **extra}


class TestManifestPayload:
    def test_keeps_the_game_s_order(self):
        payload = manifest_payload(extract(
            page("Zulrah", [12921], ["Pet snakeling"]),
            page("Vorkath", [21907], ["Vorkath's head"]),
        ))
        assert [p["name"] for p in payload[0]["pages"]] == ["Zulrah", "Vorkath"]

    def test_drops_provenance_the_client_does_not_need(self):
        payload = manifest_payload(extract(page("Zulrah", [12921], ["Pet snakeling"],
                                                struct=476)))
        assert set(payload[0]["pages"][0]) == {"name", "items", "names"}
        assert "struct" not in payload[0]

    def test_pads_names_to_match_items(self):
        # Names index alongside ids. A short list would shift every later slot's
        # label onto the wrong item rather than leaving one blank.
        payload = manifest_payload(extract(page("Zulrah", [1, 2, 3], ["one"])))
        assert payload[0]["pages"][0]["names"] == ["one", "", ""]

    def test_truncates_names_that_overrun_items(self):
        payload = manifest_payload(extract(page("Zulrah", [1], ["one", "two"])))
        assert payload[0]["pages"][0]["names"] == ["one"]

    def test_skips_empty_pages_and_tabs(self):
        assert manifest_payload(extract(page("Nothing", []))) == []


class TestSlotHelpers:
    def test_slot_ids_spans_tabs_and_pages(self):
        structure = manifest_payload(extract(
            page("Zulrah", [12921], ["Pet snakeling"]),
            page("Vorkath", [21907], ["Vorkath's head"]),
        ))
        assert slot_ids(structure) == {12921, 21907}

    def test_slot_names_keeps_the_first_page_s_name(self):
        # Pets appear on their boss page and again under "All Pets"; either
        # name is the same item, so the first one wins rather than the last.
        structure = manifest_payload(extract(
            page("Zulrah", [12921], ["Pet snakeling"]),
            page("All Pets", [12921], ["Pet snakeling"]),
        ))
        assert slot_names(structure) == {12921: "Pet snakeling"}


class TestNameDerivedIds:
    """The audit's one judgement call.

    A full read reports ids from the game and is always right. A single unlock
    is announced by a chat message carrying only a *name*, which the plugin
    resolves against RuneLite's item cache — returning the earliest id sharing
    that name, which for a duplicated name is the wrong item.
    """

    structure = manifest_payload(extract(page("Kraken", [25627], ["Coal bag"])))

    def test_recognises_a_wrong_id_by_its_name(self):
        # The collection log's Coal bag is 25627; the item cache answers 764.
        noise, candidates = name_derived_ids(
            self.structure, {764: 7}, {764: "Coal bag"}
        )
        assert candidates == []
        assert noise == [(764, 7, "Coal bag", [25627])]

    def test_an_unknown_name_is_a_real_candidate(self):
        # This is the signal: an id nothing explains is a slot the extract has
        # not been refreshed for.
        noise, candidates = name_derived_ids(
            self.structure, {30805: 55}, {30805: "Dossier"}
        )
        assert noise == []
        assert candidates == [(30805, 55, "Dossier")]

    def test_an_id_with_no_name_at_all_stays_a_candidate(self):
        # Our items table lags game updates, so a nameless id is exactly the
        # case that must not be quietly filed as noise.
        noise, candidates = name_derived_ids(self.structure, {99999: 3}, {})
        assert noise == []
        assert candidates == [(99999, 3, "")]

    def test_defined_slots_are_not_reported_at_all(self):
        noise, candidates = name_derived_ids(
            self.structure, {25627: 400}, {25627: "Coal bag"}
        )
        assert (noise, candidates) == ([], [])

    def test_matching_ignores_case_and_padding(self):
        noise, _ = name_derived_ids(
            self.structure, {764: 1}, {764: "  coal BAG "}
        )
        assert noise[0][3] == [25627]

    def test_candidates_come_first_by_holder_count(self):
        noise, candidates = name_derived_ids(
            self.structure,
            {30805: 55, 99999: 3, 764: 7},
            {30805: "Dossier", 764: "Coal bag"},
        )
        assert [c[0] for c in candidates] == [30805, 99999]
        assert [n[0] for n in noise] == [764]
