"""services/status_channel.py — card renderers, with fake V2 component classes
injected under the stubbed ``interactions`` package."""

import importlib
import sys
import types

import pytest

# The stubbed ``services`` parent package would swallow attribute imports;
# import_module resolves the real module conftest registered in sys.modules.
sc = importlib.import_module("services.status_channel")


class FakeText:
    def __init__(self, content=""):
        self.content = content


class FakeSeparator:
    def __init__(self, divider=False):
        self.divider = divider


class FakeContainer:
    def __init__(self, *children, accent_color=None):
        self.children = list(children)
        self.accent_color = accent_color


@pytest.fixture(autouse=True)
def fake_components(monkeypatch):
    mod = types.ModuleType("interactions.models")
    mod.ContainerComponent = FakeContainer
    mod.SeparatorComponent = FakeSeparator
    mod.TextDisplayComponent = FakeText
    monkeypatch.setitem(sys.modules, "interactions.models", mod)


def _texts(container):
    return "\n".join(c.content for c in container.children if isinstance(c, FakeText))


def _snapshot(api_status="operational", webhook_status="operational", **api_extra):
    api = {
        "status": api_status,
        "online": api_status != "offline",
        "players_1h": 412,
        "processed": {"5m": 1204, "30m": 7893, "24h": 214556},
        "queue_depth": 3,
        "consumer_alive": True,
    }
    api.update(api_extra)
    return {
        "generated_at": 1_800_000_000,
        "api": api,
        "webhook": {
            "status": webhook_status,
            "online": webhook_status == "operational",
            "players_1h": 88,
            "processed": {"5m": 210, "30m": 1405, "24h": 40332},
        },
    }


def test_services_card_operational():
    (card,) = sc.build_services_components(_snapshot())
    text = _texts(card)
    assert card.accent_color == sc._ACCENT["operational"]
    assert "Submission API** — Operational" in text
    assert "**412** players active" in text
    assert "**1,204** (5m) · **7,893** (30m) · **214,556** (24h)" in text
    assert "Webhook Processing** — Operational" in text
    assert "<t:1800000000:R>" in text


def test_services_card_degraded_backlog_line():
    (card,) = sc.build_services_components(
        _snapshot(api_status="degraded", queue_depth=1234)
    )
    text = _texts(card)
    assert card.accent_color == sc._ACCENT["degraded"]
    assert "backlog: **1,234**" in text


def test_services_card_degraded_consumer_stalled():
    (card,) = sc.build_services_components(
        _snapshot(api_status="degraded", consumer_alive=False)
    )
    assert "consumer is not responding" in _texts(card)


def test_services_card_offline_wins_accent():
    (card,) = sc.build_services_components(_snapshot(webhook_status="offline"))
    text = _texts(card)
    assert card.accent_color == sc._ACCENT["offline"]
    assert "Webhook Processing** — Offline" in text
    assert "webhook reader is offline" in text


def test_issues_card_empty():
    (card,) = sc.build_issues_components([], updated_ts=1_800_000_000)
    text = _texts(card)
    assert "No known issues right now" in text
    assert card.accent_color == 0x2ECC71


def test_issues_card_sections_and_severities():
    cats = [
        {
            "name": "Plugin",
            "emoji": "🧩",
            "issues": [
                {"title": "KC not tracked for Yama", "description": "Fix in review.",
                 "severity": "major", "status": "open", "created_ts": 1_799_000_000},
                {"title": "Pet embeds missing icon", "description": None,
                 "severity": "minor", "status": "monitoring", "created_ts": None},
            ],
        },
        {"name": "Empty category", "emoji": None, "issues": []},
        {
            "name": "Website",
            "emoji": None,
            "issues": [
                {"title": "Slow lootboard page", "description": "",
                 "severity": "degraded", "status": "open", "created_ts": None},
            ],
        },
    ]
    (card,) = sc.build_issues_components(cats, updated_ts=1_800_000_000)
    text = _texts(card)
    assert "### 🧩 Plugin" in text
    assert "🔴 **KC not tracked for Yama**" in text
    assert "-# Fix in review." in text
    assert "Known since <t:1799000000:d>" in text
    assert "_(monitoring)_" in text
    assert "### Website" in text
    assert "🟠 **Slow lootboard page**" in text
    assert "Empty category" not in text
    assert card.accent_color == 0xE67E22


def test_issues_card_truncates_on_char_budget():
    cats = [{
        "name": "Bulk",
        "emoji": None,
        "issues": [
            {"title": f"Issue {i} " + "x" * 200, "description": "y" * 200,
             "severity": "minor", "status": "open", "created_ts": None}
            for i in range(40)
        ],
    }]
    (card,) = sc.build_issues_components(cats, updated_ts=1)
    text = _texts(card)
    assert "more" in text
    assert len(text) < sc._ISSUES_CHAR_BUDGET + 1000


def test_worst_status_ranking():
    assert sc._worst_status({"api": {"status": "operational"},
                            "webhook": {"status": "operational"}}) == "operational"
    assert sc._worst_status({"api": {"status": "degraded"},
                            "webhook": {"status": "operational"}}) == "degraded"
    assert sc._worst_status({"api": {"status": "operational"},
                            "webhook": {"status": "offline"}}) == "offline"
    assert sc._worst_status({"api": {}, "webhook": {}}) == "offline"
