"""Regression guards for bots/heartbeat.py's webhook-pool handling (2026-09-15).

The heartbeat bot looked fetched webhooks up by URL, but Discord withholds the
token (so interactions' Webhook.url is None) for webhooks another app created.
That queued NULL-url pending-deletion rows, and a cleanup loop deleted each
row's whole channel 96h later — one pool channel every 4 days. Separately, its
in-process restart loop reused the Client and died on "Duplicate Command!".

Importing the module under the conftest stubs would turn every decorated
function into a MagicMock, so these tests read the source instead and exec only
the pure helpers.
"""
import ast
import os
from types import SimpleNamespace

HEARTBEAT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bots", "heartbeat.py"
)
with open(HEARTBEAT) as f:
    SOURCE = f.read()
TREE = ast.parse(SOURCE)


def _functions():
    return [node for node in ast.walk(TREE) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _enclosing(target):
    for function in _functions():
        if any(node is target for node in ast.walk(function)):
            return function
    return None


def _pure(*names):
    wanted = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in wanted} == set(names)
    namespace = {}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), HEARTBEAT, "exec"), namespace)
    return namespace


class TestNeverDeletesPoolChannels:
    def test_only_a_channel_it_just_created_is_ever_deleted(self):
        offenders = []
        for node in ast.walk(TREE):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "delete"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id.endswith("channel")):
                function = _enclosing(node)
                if function is None or function.name != "create_new_webhook":
                    offenders.append((node.lineno, function.name if function else None))
        assert offenders == []

    def test_cleanup_loop_is_gone(self):
        assert "pending_deletion_cleanup_loop" not in {function.name for function in _functions()}


class TestWebhooksMatchedById:
    def test_no_lookup_keys_on_a_fetched_webhooks_url(self):
        offenders = []
        for node in ast.walk(TREE):
            # filter_by(webhook_url=webhook.url)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "filter_by":
                for keyword in node.keywords:
                    if keyword.arg == "webhook_url" and isinstance(keyword.value, ast.Attribute) and keyword.value.attr == "url":
                        offenders.append(node.lineno)
            # Model.webhook_url == webhook_data['url']
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Attribute) and node.left.attr == "webhook_url":
                for comparator in node.comparators:
                    if isinstance(comparator, ast.Subscript) and "url" in ast.unparse(comparator.slice):
                        offenders.append(node.lineno)
        assert offenders == []

    def test_untracked_matches_on_id_even_when_discord_hides_the_url(self):
        untracked_webhooks = _pure("untracked_webhooks")["untracked_webhooks"]
        hooks = [SimpleNamespace(id=1377677447021199371, url=None), SimpleNamespace(id=42, url=None)]
        assert untracked_webhooks(hooks, {"1377677447021199371"}) == [hooks[1]]


class TestRestartsWithAFreshProcess:
    def test_astart_is_never_retried_in_process(self):
        calls = [node for node in ast.walk(TREE)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "astart"]
        assert len(calls) == 1
        function = _enclosing(calls[0])
        assert not any(isinstance(node, (ast.While, ast.For, ast.AsyncFor)) for node in ast.walk(function))

    def test_not_ready_seconds(self):
        not_ready_seconds = _pure("not_ready_seconds")["not_ready_seconds"]
        assert not_ready_seconds(True, 100.0, 500.0) == (0.0, 500.0)
        assert not_ready_seconds(False, 100.0, 500.0) == (400.0, 100.0)
