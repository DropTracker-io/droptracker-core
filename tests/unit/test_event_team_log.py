"""Team submission log fold (t62).

The old team "Recent activity" feed rendered one line per applied ledger row.
On a kill-count or GP task that is a line per kill/drop, so the drops people
actually want to see ("who got what, when") were buried under progress ticks.
:func:`_fold_team_log` is the noise reduction: acquisitions keep their line,
ticks roll up per (player, task).
"""

from web_api.routes import events as ev_routes


def _entry(cid, *, task_id=1, task_type="kc_target", points=0, completed=False,
           quantity=1, created_at=1000, source_type="drop", note=None,
           matched_target=None, item_id=None, proof_url=None):
    return {
        "completion_id": cid,
        "task_id": task_id,
        "task_label": f"task {task_id}",
        "task_type": task_type,
        "player_id": 7,
        "player_name": "Zezima",
        "hidden": False,
        "matched_target": matched_target,
        "item_id": item_id,
        "quantity": quantity,
        "points": points,
        "completed": completed,
        "source_type": source_type,
        "note": note,
        "proof_url": proof_url,
        "created_at": created_at,
    }


def _fold(rows):
    """`rows` as (player_id, entry) pairs on one task, oldest-first."""
    return ev_routes._fold_team_log([((pid, e["task_id"]), e) for pid, e in rows])


class TestMetricTicksRollUp:
    def test_a_run_of_kc_ticks_becomes_one_line(self):
        rows = [(7, _entry(i, quantity=1, created_at=1000 + i)) for i in range(1, 51)]
        entries, folded = _fold(rows)
        assert folded == 50
        assert len(entries) == 1
        line = entries[0]
        assert line["collapsed"] == 50
        assert line["quantity"] == 50
        # Newest tick times the line; the oldest anchors "since".
        assert line["created_at"] == 1050
        assert line["collapsed_since"] == 1001

    def test_a_run_spanning_many_drops_names_no_item_and_shows_no_proof(self):
        rows = [
            (7, _entry(1, matched_target="Coins", item_id=995, proof_url="https://x/1.png",
                       task_type="loot_value", quantity=10, created_at=1001)),
            (7, _entry(2, matched_target="Bones", item_id=526, proof_url="https://x/2.png",
                       task_type="loot_value", quantity=20, created_at=1002)),
        ]
        entries, _ = _fold(rows)
        assert len(entries) == 1
        assert entries[0]["matched_target"] is None
        assert entries[0]["item_id"] is None
        assert entries[0]["proof_url"] is None
        assert entries[0]["quantity"] == 30

    def test_two_players_on_one_task_stay_separate(self):
        rows = [
            (7, _entry(1, created_at=1001)),
            (9, _entry(2, created_at=1002)),
            (7, _entry(3, created_at=1003)),
        ]
        entries, folded = _fold(rows)
        assert folded == 3
        assert len(entries) == 2
        assert sorted(e["collapsed"] for e in entries) == [1, 2]

    def test_two_masked_players_never_merge(self):
        # Masked rows both reach a public viewer as (None, "Hidden player"),
        # so the fold key must be the REAL id — which is what the route feeds
        # in. Same display, different key => two lines.
        a = _entry(1, created_at=1001)
        b = _entry(2, created_at=1002)
        for e in (a, b):
            e["player_id"], e["player_name"], e["hidden"] = None, "Hidden player", True
        entries, _ = ev_routes._fold_team_log([((7, 1), a), ((9, 1), b)])
        assert len(entries) == 2


class TestRealContributionsKeepTheirLine:
    def test_an_item_row_is_never_folded(self):
        rows = [
            (7, _entry(1, task_type="item_collection", matched_target="Twisted bow",
                       item_id=20997, proof_url="https://x/1.png", created_at=1001)),
            (7, _entry(2, task_type="item_collection", matched_target="Elysian sigil",
                       item_id=12819, proof_url="https://x/2.png", created_at=1002)),
        ]
        entries, folded = _fold(rows)
        assert folded == 0
        assert [e["matched_target"] for e in entries] == ["Elysian sigil", "Twisted bow"]
        assert all(e["proof_url"] for e in entries)

    def test_the_tick_that_scored_stands_alone(self):
        rows = [
            (7, _entry(1, created_at=1001)),
            (7, _entry(2, created_at=1002)),
            # Crossed the threshold — flagged by the fold, not inferred from
            # the points it happened to credit.
            (7, _entry(3, points=25, completed=True, created_at=1003)),
            (7, _entry(4, created_at=1004)),
        ]
        entries, folded = _fold(rows)
        assert folded == 3
        # The scoring row plus one rollup for the run around it.
        assert [(e["completion_id"], e.get("collapsed")) for e in entries] == [
            (4, 3), (3, None),
        ]
        assert entries[1]["points"] == 25

    def test_a_zero_point_completion_stands_alone_too(self):
        # A task worth 0 points (a bingo tile scored only by line/blackout
        # bonuses) still FINISHES: its crossing row must not be buried in an
        # "advanced N times" rollup just because it credited nothing.
        rows = [
            (7, _entry(1, created_at=1001)),
            (7, _entry(2, points=0, completed=True, created_at=1002)),
            (7, _entry(3, created_at=1003)),
        ]
        entries, folded = _fold(rows)
        assert folded == 2
        # The ticks roll up into the one line this key gets; the completion
        # keeps its own.
        assert [(e["completion_id"], e.get("collapsed")) for e in entries] == [
            (3, 2), (2, None),
        ]

    def test_manual_and_bonus_awards_stand_alone(self):
        rows = [
            (7, _entry(1, source_type="manual", note="clan pot payout", created_at=1001)),
            (7, _entry(2, source_type="bonus", created_at=1002)),
        ]
        entries, folded = _fold(rows)
        assert folded == 0
        assert len(entries) == 2

    def test_a_noted_tick_is_not_swallowed(self):
        # `note` only ever carries an organizer's reason; hiding it behind a
        # count would lose the only explanation of an odd row.
        rows = [
            (7, _entry(1, created_at=1001)),
            (7, _entry(2, note="awarded manually after a plugin outage", created_at=1002)),
        ]
        entries, folded = _fold(rows)
        assert folded == 1
        assert len(entries) == 2


class TestOrdering:
    def test_a_rollup_sorts_by_its_newest_tick(self):
        # The run STARTED before the drop but is still going, so it must not
        # sink to the bottom of the log at its first tick's timestamp.
        rows = [
            (7, _entry(1, created_at=1001)),
            (7, _entry(2, task_id=2, task_type="item_collection",
                       matched_target="Twisted bow", created_at=1002)),
            (7, _entry(3, created_at=1003)),
        ]
        entries, _ = _fold(rows)
        assert [e["completion_id"] for e in entries] == [3, 2]

    def test_newest_first(self):
        rows = [
            (7, _entry(i, task_type="item_collection", matched_target=f"Item {i}",
                       created_at=1000 + i))
            for i in range(1, 4)
        ]
        entries, _ = _fold(rows)
        assert [e["completion_id"] for e in entries] == [3, 2, 1]

    def test_empty_ledger_folds_to_nothing(self):
        assert ev_routes._fold_team_log([]) == ([], 0)


def test_the_log_endpoint_is_registered():
    from quart import Quart

    app = Quart(__name__)
    app.register_blueprint(ev_routes.events_bp)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert "/events/<int:event_id>/teams/<int:team_id>/contributions" in rules
