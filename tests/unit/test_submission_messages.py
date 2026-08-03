"""Unit tests for utils/submission_messages.py — the player-facing wording
for rejected manual submissions.

Regression origin: a player manually submitting coins from Tombs of Amascut
was shown the literal string "Intake returned 200.". Two bugs stacked up —
the wiki drop-source check falsely rejected the drop (fixed in
osrs_api/semantic.py), and the website read only the ``error`` key from a
rejection payload that carries its reason in ``message``, so it fell through
to a status-code fallback.
"""

import pytest

from utils.submission_messages import (
    GENERIC_REJECTION,
    SERVICE_UNAVAILABLE,
    friendly_rejection,
    rejection_reason,
)


# ── rejection_reason ─────────────────────────────────────────────────────────
# /manual-submit answers a processor rejection with HTTP 200 and
# {"success": false, "message": ...}; only its own validation/parse failures
# set "error". Both must be found.

def test_reason_prefers_error_key():
    assert rejection_reason({"error": "Unauthorized.", "message": "ignored"}) == "Unauthorized."


def test_reason_falls_back_to_message():
    """The exact payload shape that produced "Intake returned 200."."""
    body = {
        "success": False,
        "status": "rejected",
        "message": "Item Coins is not from NPC Tombs of Amascut",
        "notice": None,
    }
    assert rejection_reason(body) == "Item Coins is not from NPC Tombs of Amascut"


def test_reason_falls_back_to_notice():
    assert rejection_reason({"message": None, "notice": "Held for review"}) == "Held for review"


@pytest.mark.parametrize(
    "body",
    [{}, {"message": ""}, {"message": "   "}, {"message": None}, None, "not a dict", []],
)
def test_reason_none_when_unexplained(body):
    assert rejection_reason(body) is None


# ── friendly_rejection ───────────────────────────────────────────────────────

def test_no_reason_uses_fallback():
    assert friendly_rejection({}) == GENERIC_REJECTION
    assert friendly_rejection({}, fallback="custom") == "custom"


def test_never_returns_the_status_code_fallback_for_a_real_rejection():
    """The actual bug: a 200-with-success-false must yield the reason, not
    a fallback about the transport."""
    body = {"success": False, "message": "Item Coins is not from NPC Tombs of Amascut"}
    result = friendly_rejection(body, fallback="Intake returned 200.")
    assert result != "Intake returned 200."
    assert "Coins" in result and "Tombs of Amascut" in result


@pytest.mark.parametrize(
    "message,expected_fragments",
    [
        # drop.py — the >1M wiki drop-source check
        (
            "Item Coins is not from NPC Tombs of Amascut",
            ["couldn't verify", "Coins", "Tombs of Amascut"],
        ),
        ("Item was not found in the database", ["don't recognise that item"]),
        ("Item Bandos chestplate not found in the database", ["Bandos chestplate"]),
        (
            "NPC ID could not be resolved for Sheep, aborting",
            ["Sheep", "boss or NPC"],
        ),
        (
            "Player Zezima not found in the database",
            ["isn't registered", "RuneLite plugin"],
        ),
        ("Player not found or could not be created.", ["isn't registered"]),
        ("Player Zezima failed auth check", ["belongs to you"]),
        ("Player authentication failed.", ["belongs to you"]),
        ("Missing required player identification fields.", ["which account"]),
        (
            "Drop value exceeds the plausible maximum and was rejected",
            ["higher than a single drop", "quantity"],
        ),
        ("Invalid drop quantity", ["whole number of at least 1"]),
        ("Failed to create drop", ["went wrong", "Try again"]),
    ],
)
def test_pipeline_jargon_is_rewritten(message, expected_fragments):
    result = friendly_rejection({"success": False, "message": message})
    assert result != message, "message should have been rewritten"
    for fragment in expected_fragments:
        assert fragment in result, f"{fragment!r} missing from {result!r}"


def test_item_not_found_does_not_capture_the_word_was():
    """Rule ordering guard: the exact-wording rule must win, or the generic
    'Item was not found...' reads as an item literally named "was"."""
    assert "was isn't in our item list" not in friendly_rejection(
        {"message": "Item was not found in the database"}
    )


def test_processor_traceback_is_never_shown_to_a_player():
    body = {
        "success": False,
        "error": "Error processing submission: KeyError('acc_hash')",
    }
    result = friendly_rejection(body)
    assert "KeyError" not in result
    assert "went wrong" in result


@pytest.mark.parametrize(
    "reason",
    ["Unauthorized.", "Unauthorized", "Manual submissions are not configured on the server."],
)
def test_shared_secret_gate_reads_as_a_service_problem(reason):
    """A player can't act on either, so neither should sound like their fault."""
    assert friendly_rejection({"error": reason}) == SERVICE_UNAVAILABLE


def test_unrecognised_reason_passes_through():
    """A blunt real reason still beats a generic one."""
    body = {"success": False, "message": "Something unmapped happened"}
    assert friendly_rejection(body) == "Something unmapped happened"
