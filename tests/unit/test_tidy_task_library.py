"""Decision core of scripts/tidy_task_library.py — which public presets a
tidy pass retires. Pure (rows + a validate callback), so no DB."""

from scripts.tidy_task_library import plan


def _row(id, name, source="group", type="item_collection"):
    return {"id": id, "name": name, "source": source, "type": type,
            "target": None, "target_value": None, "config": None}


def test_duplicates_keep_automated_then_source_then_oldest():
    rows = [
        _row(5, "Any Zulrah Unique"),
        _row(2, "any zulrah unique", source="legacy_v1", type="custom"),
        _row(9, "Any Zulrah Unique", source="curated"),
    ]
    out = plan(rows, lambda r: True)
    retired = {r["id"] for r, _ in out["duplicates"]}
    keepers = {k["id"] for _, k in out["duplicates"]}
    # The curated automated row beats the manual-only legacy one.
    assert keepers == {9}
    assert retired == {2, 5}


def test_broken_rows_retire_but_never_double_count_duplicates():
    rows = [_row(1, "A"), _row(2, "A"), _row(3, "B")]
    out = plan(rows, lambda r: r["id"] != 2 and r["id"] != 3)
    assert [r["id"] for r, _ in out["duplicates"]] == [2]
    assert [r["id"] for r in out["broken"]] == [3]


def test_legacy_custom_rows_are_reported_not_retired():
    rows = [_row(1, "100 Fish", source="legacy_v1", type="custom"), _row(2, "Cabbage")]
    out = plan(rows, lambda r: True)
    assert [r["id"] for r in out["placeholders"]] == [1]
    assert not out["duplicates"] and not out["broken"]
