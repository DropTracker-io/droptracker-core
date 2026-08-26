"""Telemetry must never fire from a test run, and must not lie about where it ran.

On 2026-08-26 three Sentry issues (DROPTRACKER-24/25/26) were raised against
production that no production process had emitted. They came from a green
pytest run: ``init_sentry()`` is called at *module import* time in eleven
modules, and it calls ``load_dotenv()`` itself, so a single
``import workers.webhook_consumer`` in one test module armed Sentry with the
production DSN for the whole pytest process. Sentry's default
LoggingIntegration then shipped every ``log.error()`` the suite deliberately
provokes — dead-lettering, requeue failure — as a production incident, tagged
``environment=production`` and indistinguishable from a real one.
"""

import importlib

import pytest

import utils.sentry as sentry


class TestPytestGuard:
    def test_init_is_a_noop_under_pytest(self, monkeypatch):
        # A real DSN present and sentry-sdk installed: the only thing standing
        # between the suite and the production Sentry project is this guard.
        monkeypatch.setattr(sentry, "_initialized", False)
        monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")

        assert sentry.init_sentry("droptracker-test") is False

    def test_the_guard_does_not_depend_on_the_dsn_being_absent(self, monkeypatch):
        """conftest clearing SENTRY_DSN would not be enough on its own.

        ``init_sentry`` calls ``load_dotenv()`` itself, so it reads the DSN out
        of .env regardless of what the test bootstrap set up.
        """
        monkeypatch.setattr(sentry, "_initialized", False)
        monkeypatch.delenv("SENTRY_DSN", raising=False)

        assert sentry.init_sentry("droptracker-test") is False

    def test_importing_a_worker_does_not_arm_sentry(self):
        """The specific import that caused DROPTRACKER-24/25/26."""
        sentry_sdk = pytest.importorskip("sentry_sdk")
        importlib.import_module("workers.webhook_consumer")

        assert sentry_sdk.get_client().is_active() is False


class TestEnvironmentName:
    """`environment` used to be hardcoded to "production", so a dev box on the
    same DSN was indistinguishable from the live fleet."""

    def test_live_is_production(self, monkeypatch):
        monkeypatch.setenv("STATE", "live")
        assert sentry._environment_name() == "production"

    def test_the_shipped_env_quotes_the_value(self, monkeypatch):
        # .env writes STATE="live"; python-dotenv usually strips the quotes,
        # but web_api/billing.py strips them defensively and so do we.
        monkeypatch.setenv("STATE", '"live"')
        assert sentry._environment_name() == "production"

    def test_dev_is_not_production(self, monkeypatch):
        monkeypatch.setenv("STATE", "dev")
        assert sentry._environment_name() == "dev"

    def test_unset_state_is_not_production(self, monkeypatch):
        monkeypatch.delenv("STATE", raising=False)
        assert sentry._environment_name() == "unknown"
