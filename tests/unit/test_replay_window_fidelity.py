"""Fidelity contract for scripts/replay_webhook_window.py.

The replay recovers webhook-path submissions by re-dispatching Discord messages
through the live intake code. Two properties make it a recovery tool rather
than a second, subtly different intake path — and both are easy to break by
accident, so they are pinned here.

1. **It must not fork the bundling logic.** It calls the reader's own
   ``build_message_bundle``. A private reimplementation would drift from intake
   exactly the way the per-transport dispatch copies did before
   ``data/submissions/dispatch.py`` unified them.

2. **The payloads it hands over must be the ones it prepared.** The replay
   stamps each payload with ``_received_at`` from the Discord message so a
   recovered row keeps its original time instead of being dated to whenever the
   backfill ran. That was silently undone once already: the dispatch helper
   rebuilt the bundle from the message internally, throwing the stamps away and
   re-dating ~10k recovered rows to the replay's own clock.

Source-scanned rather than imported: ``bots/webhook_bot.py`` pulls in the whole
Discord + DB stack, which is why the dispatch parity suite reads it the same way.
"""

import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
READER = os.path.join(REPO_ROOT, "bots", "webhook_bot.py")
REPLAY = os.path.join(REPO_ROOT, "scripts", "replay_webhook_window.py")


def _func(path, name):
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


class TestDispatchHonoursTheCallersBundle:
    def test_process_message_bundle_accepts_a_bundle(self):
        fn = _func(READER, "process_message_bundle")
        names = [a.arg for a in fn.args.args]
        assert "bundle" in names, (
            "process_message_bundle must let a caller supply the payloads it "
            "already built; without it the replay's timestamps are discarded"
        )

    def test_it_does_not_rebuild_unconditionally(self):
        """A bare build_message_bundle(message) call would discard caller edits."""
        fn = _func(READER, "process_message_bundle")
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                called = getattr(node.value.func, "id", None)
                assert called != "build_message_bundle", (
                    "process_message_bundle rebuilds the bundle unconditionally, "
                    "silently throwing away whatever the caller passed in"
                )

    def test_replay_passes_its_bundle_through(self):
        fn = _func(REPLAY, "_scan")
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "process_message_bundle"
        ]
        assert calls, "the replay no longer dispatches via process_message_bundle"
        for call in calls:
            assert len(call.args) >= 2, (
                "the replay must hand its stamped bundle to process_message_bundle, "
                "otherwise recovered rows are re-dated to the backfill's clock"
            )


class TestReplayReusesIntake:
    def test_replay_uses_the_readers_bundler(self):
        src = open(REPLAY).read()
        assert "build_message_bundle" in src, (
            "the replay must reuse the reader's bundling, not reimplement it"
        )

    def test_replay_stamps_the_original_receive_time(self):
        src = open(REPLAY).read()
        assert "_received_at" in src, (
            "the replay must stamp payloads with the Discord message time so "
            "recovered rows keep their original date_added"
        )

    def test_replay_defaults_to_a_dry_run(self):
        """Matches the repo-wide maintenance-script idiom."""
        src = open(REPLAY).read()
        assert '"--apply"' in src and "action=\"store_true\"" in src


class TestGuidDedupIsTransportBlind:
    """A GUID identifies the submission, not the transport that carried it.

    `ensure_can_create`'s drop lookup filtered on `used_api == True`, but
    webhook-path drops are written `used_api=False` — so the lookup could not
    see them and every replayed webhook drop wrote a second row. Replaying the
    2026-08-18 outage duplicated 35,619 drops before it was caught. Any
    `used_api` term in these lookups reintroduces that.
    """

    def _check_existing_source(self):
        path = os.path.join(REPO_ROOT, "data", "submissions", "common.py")
        fn = _func(path, "ensure_can_create")
        for node in ast.walk(fn):
            if isinstance(node, ast.FunctionDef) and node.name == "_check_existing":
                return ast.dump(node)
        raise AssertionError("_check_existing not found inside ensure_can_create")

    def test_no_used_api_filter_in_any_dedup_lookup(self):
        assert "used_api" not in self._check_existing_source(), (
            "a used_api term makes the GUID lookup transport-specific, so a "
            "replayed submission cannot match its own original and is written twice"
        )
