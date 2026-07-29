"""
Test bootstrap: sets environment variables and stubs heavy modules in sys.modules
BEFORE any app code is imported, so tests can run without a live DB/Redis/Discord.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Environment variables (must come before any app imports) ──────────────────
os.environ.setdefault("DB_USER", "test_user")
os.environ.setdefault("DB_PASS", "test_pass")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_NAME", "test_data")
os.environ.setdefault("DISCORD_MESSAGE_FOOTER", "DropTracker | droptracker.io")
os.environ.setdefault("BOT_TOKEN", "fake-bot-token")
os.environ.setdefault("DEV_TOKEN", "fake-dev-token")
os.environ.setdefault("WOM_API_KEY", "fake-wom-key")
os.environ.setdefault("ENCRYPTION_KEY", "fake-enc-key")
os.environ.setdefault("STATE", "dev")
os.environ.setdefault("API_PORT", "31323")
os.environ.setdefault("WEBHOOK_TOKEN", "fake-webhook-token")
os.environ.setdefault("XF_KEY", "fake-xf-key")

# ── Stub heavy modules BEFORE any test module imports them ────────────────────
# These modules either connect to DB/Redis at import time or are very large;
# replacing them with MagicMock prevents import failures in unit tests.
_STUBS = [
    # Database layer
    "db",
    "db.models",
    "db.models.base",
    "db.models.drop",
    "db.models.player",
    "db.models.user",
    "db.models.group",
    "db.models.guild",
    "db.models.notification_queue",
    "db.models.notified_submission",
    "db.models.group_configuration",
    "db.models.collection",
    "db.models.personal_best",
    "db.models.combat_achievement",
    "db.models.player_pet",
    "db.models.quest_completion",
    "db.models.recap",
    "db.models.seasonal_drop",
    "db.models.seasonal_personal_best",
    "db.models.seasonal_collection",
    "db.models.seasonal_combat_achievement",
    "db.models.seasonal_player_pet",
    "db.models.seasonal_quest_completion",
    "db.models.group_points",
    "db.models.premium_features",
    "db.models.analytics",
    "db.models.embed",
    "db.models.webhooks",
    "db.models.associations",
    "db.models.drop_split",
    "db.models.video_upload",
    "db.models.user_config",
    "db.ops",
    "db.app_logger",
    "db.clan_sync",
    "db.group_creation",
    "db.xf",
    "db.xf.recent_submissions",
    "db.xf.upgrades",
    # API core (creates loggers/metrics at import)
    "api.core",
    "api.services",
    "api.services.metrics",
    # Redis
    "utils.redis",
    # External APIs
    "utils.wiseoldman",
    "utils.ge_value",
    "utils.download",
    "utils.logger",
    "utils.b2_storage",
    "utils.video_storage",
    "utils.messages",
    "utils.encrypter",
    "utils.patreon",
    "utils.cloudflare_update",
    "utils.github",
    "osrs_api",
    # Service layer
    "services",
    "services.redis_updates",
    "services.points",
    "services.nitro_attribution",
    "services.submission_status",
    "services.seasonal_state",
    "services.event_types",
    "services.item_totals",
    "services.notification_service",
    "services.hall_of_fame",
    "services.message_handler",
    "services.channel_names",
    "services.components",
    "services.entry_modifier",
    "services.user_context",
    "services.group_poll",
    "services.ticket_system",
    "services.video_worker",
    "services.wiki",
    "services.xf_services",
    "services.bot_state",
    # Discord library
    "interactions",
    # Monitor / systemd integration
    "monitor",
    "monitor.sdnotifier",
]

for _stub_name in _STUBS:
    if _stub_name not in sys.modules:
        sys.modules[_stub_name] = MagicMock()

# ── Real modules that live under stubbed packages ─────────────────────────────
# db/entitlements.py is pure logic (stdlib-only module imports; DB access is
# lazy inside functions), but `import db.entitlements` would execute the real
# db/__init__.py. Load it by file path under its dotted name so unit tests can
# exercise the entitlement registry (web_api.entitlements_registry re-exports it).
import importlib.util as _importlib_util
from pathlib import Path as _Path

_ENTITLEMENTS_PATH = _Path(__file__).resolve().parent.parent / "db" / "entitlements.py"
if "db.entitlements" not in sys.modules:
    _spec = _importlib_util.spec_from_file_location("db.entitlements", _ENTITLEMENTS_PATH)
    _mod = _importlib_util.module_from_spec(_spec)
    sys.modules["db.entitlements"] = _mod
    _spec.loader.exec_module(_mod)

# db/item_sources.py — the item -> source-NPC queries, shared by the item page
# and the events worker's effort resolver. Module imports are SQLAlchemy-only
# and the ORM models are lazy-imported inside functions, so load the real
# module: web_api.routes.items re-exports its helpers and the source tests
# assert on the queries it issues.
_ITEM_SOURCES_PATH = _Path(__file__).resolve().parent.parent / "db" / "item_sources.py"
if "db.item_sources" not in sys.modules:
    _spec = _importlib_util.spec_from_file_location("db.item_sources", _ITEM_SOURCES_PATH)
    _mod = _importlib_util.module_from_spec(_spec)
    sys.modules["db.item_sources"] = _mod
    _spec.loader.exec_module(_mod)

# db/event_rate_limits.py (web65a) — same shape: stdlib-only module imports,
# lazy DB access, fail-closed grant helpers. deps/lifecycle import it lazily,
# so route tests need it resolvable under the stubbed ``db`` package.
_RATE_LIMITS_PATH = _Path(__file__).resolve().parent.parent / "db" / "event_rate_limits.py"
if "db.event_rate_limits" not in sys.modules:
    _spec = _importlib_util.spec_from_file_location("db.event_rate_limits", _RATE_LIMITS_PATH)
    _mod = _importlib_util.module_from_spec(_spec)
    sys.modules["db.event_rate_limits"] = _mod
    _spec.loader.exec_module(_mod)

# services/event_effort.py — the Bingo EHB scoring core. Pure by design
# (stdlib-only module imports, injected lookups), so load the real module: the
# effort tests assert on its relevance/EHB decisions directly.
_EFFORT_PATH = _Path(__file__).resolve().parent.parent / "services" / "event_effort.py"
if "services.event_effort" not in sys.modules:
    _spec = _importlib_util.spec_from_file_location("services.event_effort", _EFFORT_PATH)
    _mod = _importlib_util.module_from_spec(_spec)
    sys.modules["services.event_effort"] = _mod
    _spec.loader.exec_module(_mod)

# services/event_buyins.py — the buy-in <-> roster invariant (web71a). Same
# shape (stdlib-only module imports, lazy db) and MUST be registered before
# services.event_signup, which imports it at module level.
_BUYINS_PATH = _Path(__file__).resolve().parent.parent / "services" / "event_buyins.py"
if "services.event_buyins" not in sys.modules:
    _spec = _importlib_util.spec_from_file_location("services.event_buyins", _BUYINS_PATH)
    _mod = _importlib_util.module_from_spec(_spec)
    sys.modules["services.event_buyins"] = _mod
    _spec.loader.exec_module(_mod)

# services/event_signup.py — the shared sign-up rules (web70a's window gate
# among them), imported lazily by the web routes and the bot. Module imports
# are stdlib-only by design (DB models are lazy-imported inside functions), so
# load the real thing rather than a MagicMock: the route tests assert on its
# decisions.
_SIGNUP_PATH = _Path(__file__).resolve().parent.parent / "services" / "event_signup.py"
if "services.event_signup" not in sys.modules:
    _spec = _importlib_util.spec_from_file_location("services.event_signup", _SIGNUP_PATH)
    _mod = _importlib_util.module_from_spec(_spec)
    sys.modules["services.event_signup"] = _mod
    _spec.loader.exec_module(_mod)

# ── SQLAlchemy column expression stub ─────────────────────────────────────────
# Real SQLAlchemy column attributes implement comparison operators to return
# BinaryExpression objects.  When tests do `Model.date_added > cutoff`,
# Python would otherwise try datetime.__lt__(MagicMock) which raises TypeError.
class _ColExpr:
    """Lightweight stand-in for a SQLAlchemy column expression."""
    def __eq__(self, other): return _ColExpr()
    def __ne__(self, other): return _ColExpr()
    def __gt__(self, other): return _ColExpr()
    def __lt__(self, other): return _ColExpr()
    def __ge__(self, other): return _ColExpr()
    def __le__(self, other): return _ColExpr()
    def __and__(self, other): return _ColExpr()
    def __or__(self, other): return _ColExpr()
    def __bool__(self): return True
    def ilike(self, val): return _ColExpr()
    def in_(self, vals): return _ColExpr()


class _ModelMock(MagicMock):
    """MagicMock subclass where attribute access yields comparison-safe _ColExpr objects."""
    def __getattr__(self, name):
        if name.startswith("_"):
            return super().__getattr__(name)
        return _ColExpr()


# ── Wire up commonly-accessed attributes on stubs ─────────────────────────────
# The db stub must expose a usable scoped session and model classes.
_db_stub = sys.modules["db"]
_db_stub.session = MagicMock()
_db_stub.models = sys.modules["db.models"]

# Model classes referenced via `from db import Player, Drop, ...`
for _model_name in [
    "Player", "User", "Group", "Drop", "NpcList", "ItemList",
    "GroupConfiguration", "UserConfiguration", "NotificationQueue",
    "NotifiedSubmission", "CollectionLogEntry", "PersonalBestEntry",
    "CombatAchievementEntry", "PlayerPet", "QuestCompletionEntry",
    "FeatureActivation", "SeasonalDrop", "SeasonalPersonalBestEntry",
    "SeasonalCollectionLogEntry", "SeasonalCombatAchievementEntry",
    "SeasonalPlayerPet", "SeasonalQuestCompletionEntry",
    "user_group_association",
]:
    setattr(_db_stub, _model_name, _ModelMock(name=_model_name))

# api.core stub needs a logger attribute
sys.modules["api.core"].logger = MagicMock()

# utils.wiseoldman stubs – return 4-tuples matching check_user_by_username signature
_wom_stub = sys.modules["utils.wiseoldman"]
_wom_stub.check_user_by_username = AsyncMock(return_value=(None, None, None, -1))
_wom_stub.check_user_by_id = AsyncMock(return_value=(None, None, None, -1))
_wom_stub.check_group_by_id = AsyncMock(return_value=None)
_wom_stub.fetch_group_members = AsyncMock(return_value=[])
_wom_stub.get_collections_logged = AsyncMock(return_value=0)
_wom_stub.get_player_boss_kills = AsyncMock(return_value=0)
_wom_stub.get_player_metric = AsyncMock(return_value=0)

# utils.ge_value stub
sys.modules["utils.ge_value"].get_true_item_value = AsyncMock(return_value=0)

# services.redis_updates stub
sys.modules["services.redis_updates"].add_to_player = MagicMock()
sys.modules["services.redis_updates"].add_split_credit = MagicMock()

# services.points stub
sys.modules["services.points"].award_points_to_player = MagicMock()

# services.event_types stub — default to "everything creatable" so the
# event-create guardrail tests (scripted sessions, exact query counts) never
# hit the restricted branch's user lookup.
sys.modules["services.event_types"].creation_restricted = MagicMock(return_value=False)
sys.modules["services.event_types"].is_event_type_creatable = MagicMock(return_value=True)

# db.ops stub – DatabaseOperations and helpers
_ops_stub = sys.modules["db.ops"]
_ops_stub.DatabaseOperations = MagicMock()
_ops_stub.associate_player_ids = MagicMock()
_ops_stub.get_point_divisor = MagicMock(return_value=1000000)


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def mock_session():
    """A mock SQLAlchemy session that returns None for all queries by default."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    session.query.return_value.filter.return_value.all.return_value = []
    session.query.return_value.all.return_value = []
    return session


@pytest.fixture
def mock_player():
    """A pre-built mock Player object with sensible defaults."""
    player = MagicMock()
    player.player_id = 42
    player.player_name = "TestPlayer"
    player.account_hash = "testhash123"
    player.wom_id = 12345
    player.total_level = 1500
    player.log_slots = 100
    player.user = None
    player.user_id = None
    return player


@pytest.fixture
def mock_group():
    """A pre-built mock Group object."""
    group = MagicMock()
    group.group_id = 5
    group.group_name = "Test Clan"
    group.wom_id = 99
    return group


@pytest.fixture(autouse=True)
def ensure_event_loop():
    """Structural order-independence guard for the whole unit suite.

    Any test that calls ``asyncio.run()`` resets the *current* event loop to
    ``None`` on exit (Python 3.10+). A later SYNC test that drives a coroutine
    by hand — ``asyncio.get_event_loop().run_until_complete(...)`` — then raises
    ``RuntimeError: There is no current event loop``, so the same test passes in
    isolation but fails in full-suite order (the recurring CI flake). This
    guarantees every test starts with a live current loop; pytest-asyncio owns
    the loop for ``async def`` tests, so this only backstops the sync ones.
    """
    import asyncio

    created = None
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except Exception:
        created = asyncio.new_event_loop()
        asyncio.set_event_loop(created)
    yield
    # Only clean up a loop we created ourselves — never touch pytest-asyncio's.
    if created is not None and not created.is_closed():
        created.close()


@pytest.fixture(autouse=True)
def reset_unique_id_cache():
    """
    Reset the module-level unique_id_cache in common.py before and after each test.
    Required because the cache is global state that persists across test runs.
    """
    try:
        import data.submissions.common as common_module
        for key in list(common_module.unique_id_cache.keys()):
            common_module.unique_id_cache[key] = []
    except Exception:
        pass
    yield
    try:
        import data.submissions.common as common_module
        for key in list(common_module.unique_id_cache.keys()):
            common_module.unique_id_cache[key] = []
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_notification_cache():
    """
    Reset stored_notifications in common.py between tests.
    """
    try:
        import data.submissions.common as common_module
        common_module.stored_notifications = {}
    except Exception:
        pass
    yield
    try:
        import data.submissions.common as common_module
        common_module.stored_notifications = {}
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_group_config_cache():
    """Clear the in-process TTL cache in utils.group_config between tests."""
    try:
        import utils.group_config as gc
        gc._cache.clear()
    except Exception:
        pass
    yield
    try:
        import utils.group_config as gc
        gc._cache.clear()
    except Exception:
        pass
