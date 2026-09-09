"""A drop withheld from every group must still say why.

The processors build a "your submission did not include a screenshot (required
for X)" notice when a group runs ``only_send_messages_with_images`` and the
submission has neither image nor video, then ``continue`` past that group.
``SubmissionResponse.notice`` is the only channel that reaches the player: the
plugin renders it as in-game chat, either straight off the /webhook response
(``SubmissionManager.onResponse``) or, in queue mode, via the inbox envelope
``submission_notice`` (``EventNotificationService``) — both gated on the
client's ``receiveInGameMessages`` config.

``drop.py`` returned it only from the ``sent_group_notifications != []``
branch. When a drop was withheld from EVERY one of the player's groups that
list is empty, control fell to the ``else``, and the response carried no notice
at all — so the players the image gate actually stops were the only ones never
told about it.

2026-09-02: player robvei (player_id 7078) in group 118 "Solana"
(``only_send_messages_with_images=1`` since 2026-07-15) had drop_id 203650477
(Broken dragon hook, 1,202,619 gp) silently withheld and reported it as a bug
after weeks of confusion. Their only two groups are the 5M-threshold global
group and Solana at 1M, so the drop cleared exactly one gate, was held there
for the missing screenshot, and left ``sent_group_notifications`` empty — the
else branch. That account has 27,603 lifetime drops of which 15 carried an
image; roughly 20 active players sit at a 0% screenshot rate on drops
over 500k.

``clog``/``ca``/``pb``/``pet`` each have a single terminal return that already
passes the notice unconditionally. This guard is static so that a future branch
in any of the five cannot silently drop the channel again.
"""

import ast
from pathlib import Path

import pytest


PROCESSOR_DIR = Path(__file__).resolve().parents[2] / "data" / "submissions"

PROCESSORS = ["drop.py", "clog.py", "ca.py", "pb.py", "pet.py"]

NOTICE_MARKER = "did not include a screenshot"


def _module(name):
    return ast.parse((PROCESSOR_DIR / name).read_text(encoding="utf-8"))


def _notice_assignment_line(tree, name):
    """Line where the screenshot notice string is built."""
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "notice" for t in node.targets
        )
        and NOTICE_MARKER in ast.unparse(node.value)
    ]
    assert lines, f"{name} no longer builds the screenshot notice — has the gate moved?"
    return min(lines)


def _submission_response_returns(tree):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "SubmissionResponse"
    ]


@pytest.mark.parametrize("name", PROCESSORS)
def test_every_return_after_the_notice_carries_it(name):
    """The exact shape of the drop.py bug, fenced for all five processors.

    A ``return SubmissionResponse(...)`` that sits after the notice has been
    built is reachable with a pending notice, so it must forward one.
    """
    tree = _module(name)
    built_at = _notice_assignment_line(tree, name)

    reachable = [r for r in _submission_response_returns(tree) if r.lineno > built_at]
    assert reachable, f"{name}: expected at least one return after the notice is built"

    for ret in reachable:
        kwargs = {kw.arg for kw in ret.value.keywords}
        assert "notice" in kwargs, (
            f"{name}:{ret.lineno} returns SubmissionResponse without a notice. "
            "A submission withheld for a missing screenshot reaching this "
            "return leaves the player with no explanation."
        )


@pytest.mark.parametrize("name", PROCESSORS)
def test_forwarded_notice_is_the_live_variable(name):
    """``notice=None`` would satisfy the kwarg check while fixing nothing."""
    tree = _module(name)
    built_at = _notice_assignment_line(tree, name)

    for ret in _submission_response_returns(tree):
        if ret.lineno <= built_at:
            continue
        for kw in ret.value.keywords:
            if kw.arg != "notice":
                continue
            assert "notice" in ast.unparse(kw.value), (
                f"{name}:{ret.lineno} passes a notice that never reads the "
                f"notice variable: {ast.unparse(kw.value)}"
            )


def test_drop_processor_both_terminal_branches_forward_the_notice():
    """The regression itself: drop.py's success path forks on whether any group
    was notified, and the empty side is the withheld-everywhere case."""
    tree = _module("drop.py")
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "drop_processor"
    )

    fork = next(
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and "sent_group_notifications" in ast.unparse(node.test)
        and node.orelse
    )

    for branch, label in ((fork.body, "notified"), (fork.orelse, "withheld-everywhere")):
        returns = [
            n
            for n in ast.walk(ast.Module(body=branch, type_ignores=[]))
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "SubmissionResponse"
        ]
        assert returns, f"drop.py: {label} branch no longer returns a SubmissionResponse"
        for ret in returns:
            assert any(kw.arg == "notice" for kw in ret.value.keywords), (
                f"drop.py:{ret.lineno} ({label} branch) drops the notice"
            )
