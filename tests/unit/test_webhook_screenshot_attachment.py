"""Screenshots must survive the Discord-webhook transport, not just the API.

The plugin's ``useApi`` option defaults to **false**, so most clients submit by
posting to a Discord webhook, which ``bots/webhook_bot.py`` reads back. On that
path the screenshot cannot arrive as a saved file — the reader only sees the
Discord CDN link, which it puts on the payload as ``attachment_url``.

``clog``/``ca``/``pb``/``pet``/``drop`` read that key. ``death``, ``quest``,
``diary`` and ``experience`` did not: they looked at ``image_url`` alone, so
every screenshot on that transport was dropped on the floor — silently, because
the row still saved and the notification still posted, just with no image.
Measured before the fix:

    death   used_api=0 -> 2803 rows, 2803 with no image  (100%)
    death   used_api=1 -> 6422 rows,   39 with no image  (0.6%)
    quest   used_api=0 ->  920 rows,  920 with no image  (100%)
    clog    used_api=0 -> 1827 rows,    5 with no image  (0.3%)

Experience keeps no per-event row, so its loss was measured on the queued
notifications themselves (30 days to 2026-08-29, transport attributed from the
player's other submissions):

    level_up + total_level_milestone, webhook players -> 980 queued, 980 with
                                                         no image  (100%)
    level_up + total_level_milestone, api players     -> 2821 queued, 128 with
                                                         no image  (4.5%)

These tests cover the shared helpers, the four processors' wiring, and a static
guard so a future submission type cannot repeat the omission.
"""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSOR_DIR = REPO_ROOT / "data" / "submissions"

DOWNLOADED_URL = "https://www.droptracker.io/img/user-upload/1234/death/Yama/Yama_7.jpg"


def _player(wom_id=1234, player_id=7):
    return SimpleNamespace(
        player_id=player_id, wom_id=wom_id, user=None, user_id=None, total_level=0
    )


def _entry(entry_id=7):
    entry = SimpleNamespace(id=entry_id, image_url=None)
    return entry


def _experience_session():
    """A session shaped for the experience processor's two kinds of read.

    The stored ``PlayerExperience`` row is a miss, so the processor creates one.
    Every ``GroupConfiguration`` read answers from one stub: ``db`` is a
    MagicMock under conftest, so the filter criteria carry no literals to
    dispatch on. ``.all()`` (the level-up branch) returns the notification
    family switched on; ``.first()`` — only the XP-milestone branch, reading
    ``notify_levels`` then ``post99_xp_interval`` — answers "1", which is both
    "on" and an interval of 1, so every milestone qualifies.
    """
    from data.submissions.common import GroupConfiguration

    config_q = MagicMock()
    config_q.filter.return_value.all.return_value = [
        SimpleNamespace(config_key="notify_levels", config_value="1")
    ]
    config_q.filter.return_value.first.return_value = SimpleNamespace(
        config_key="notify_levels", config_value="1"
    )

    other_q = MagicMock()
    other_q.filter.return_value.first.return_value = None

    session = MagicMock()
    session.query.side_effect = (
        lambda model: config_q if model is GroupConfiguration else other_q
    )
    return session


class TestAttachWebhookScreenshot:
    """The shared helper: ``common.attach_webhook_screenshot``."""

    @pytest.fixture
    def download(self, monkeypatch):
        from data.submissions import common

        mock = AsyncMock(return_value=("/local/path.jpg", DOWNLOADED_URL))
        monkeypatch.setattr(common, "download_player_image", mock)
        return mock

    async def _attach(self, session, entry, data, **kwargs):
        from data.submissions.common import attach_webhook_screenshot

        return await attach_webhook_screenshot(
            session,
            _player(),
            entry,
            data,
            submission_type=kwargs.pop("submission_type", "death"),
            entry_name=kwargs.pop("entry_name", "Yama"),
            **kwargs,
        )

    async def test_webhook_attachment_is_downloaded_onto_the_entry(self, download):
        session, entry = MagicMock(), _entry()

        url = await self._attach(
            session,
            entry,
            {"attachment_url": "https://cdn.discordapp.com/x.png",
             "attachment_type": "image/png"},
        )

        assert url == DOWNLOADED_URL
        assert entry.image_url == DOWNLOADED_URL
        assert download.await_args.kwargs["attachment_url"] == "https://cdn.discordapp.com/x.png"
        assert download.await_args.kwargs["file_extension"] == "png"
        assert download.await_args.kwargs["entry_id"] == 7

    async def test_subfolder_is_passed_through_as_npc_name(self, download):
        await self._attach(
            MagicMock(),
            _entry(),
            {"attachment_url": "https://cdn.discordapp.com/x.png"},
            subfolder="Yama",
        )
        assert download.await_args.kwargs["npc_name"] == "Yama"

    async def test_missing_subfolder_becomes_empty_not_none(self, download):
        """download_player_image joins npc_name into a path; None would blow up."""
        await self._attach(
            MagicMock(),
            _entry(),
            {"attachment_url": "https://cdn.discordapp.com/x.png"},
            subfolder=None,
        )
        assert download.await_args.kwargs["npc_name"] == ""

    async def test_api_transport_is_left_alone(self, download):
        """downloaded=True means an API path already saved the file."""
        entry = _entry()

        url = await self._attach(
            MagicMock(),
            entry,
            {"attachment_url": "https://cdn.discordapp.com/x.png", "downloaded": True},
        )

        assert url == ""
        assert entry.image_url is None
        download.assert_not_awaited()

    async def test_payload_without_an_attachment_is_a_no_op(self, download):
        entry = _entry()
        assert await self._attach(MagicMock(), entry, {}) == ""
        assert entry.image_url is None
        download.assert_not_awaited()

    async def test_external_session_is_flushed_not_committed(self, download):
        """The caller owns the transaction on the webhook/API paths."""
        session = MagicMock()

        await self._attach(
            session,
            _entry(),
            {"attachment_url": "https://cdn.discordapp.com/x.png"},
            use_external_session=True,
        )

        session.flush.assert_called_once()
        session.commit.assert_not_called()

    async def test_own_session_is_committed(self, download):
        session = MagicMock()

        await self._attach(
            session,
            _entry(),
            {"attachment_url": "https://cdn.discordapp.com/x.png"},
            use_external_session=False,
        )

        session.commit.assert_called_once()

    async def test_failed_download_costs_the_image_not_the_submission(self, monkeypatch):
        from data.submissions import common

        monkeypatch.setattr(
            common, "download_player_image", AsyncMock(side_effect=RuntimeError("cdn 404"))
        )
        session, entry = MagicMock(), _entry()

        assert await self._attach(
            session, entry, {"attachment_url": "https://cdn.discordapp.com/x.png"}
        ) == ""
        assert entry.image_url is None
        session.commit.assert_not_called()

    async def test_download_returning_nothing_leaves_the_row_untouched(self, monkeypatch):
        """download_player_image answers (None, None) on an HTTP error."""
        from data.submissions import common

        monkeypatch.setattr(
            common, "download_player_image", AsyncMock(return_value=(None, None))
        )
        entry = _entry()

        assert await self._attach(
            MagicMock(), entry, {"attachment_url": "https://cdn.discordapp.com/x.png"}
        ) == ""
        assert entry.image_url is None


class TestDownloadWebhookScreenshot:
    """The row-less helper: ``common.download_webhook_screenshot``.

    Experience submissions have nowhere to persist an image URL —
    ``PlayerExperience`` is a rolling per-player XP snapshot, not a log of
    level-ups — so this variant only hands the URL back.
    """

    @pytest.fixture
    def download(self, monkeypatch):
        from data.submissions import common

        mock = AsyncMock(return_value=("/local/path.jpg", DOWNLOADED_URL))
        monkeypatch.setattr(common, "download_player_image", mock)
        return mock

    async def _download(self, data, **kwargs):
        from data.submissions.common import download_webhook_screenshot

        return await download_webhook_screenshot(
            _player(),
            data,
            submission_type=kwargs.pop("submission_type", "experience"),
            entry_name=kwargs.pop("entry_name", "Attack"),
            entry_id=kwargs.pop("entry_id", "guid-1"),
            **kwargs,
        )

    async def test_webhook_attachment_is_downloaded_and_returned(self, download):
        url = await self._download(
            {"attachment_url": "https://cdn.discordapp.com/x.png",
             "attachment_type": "image/png"},
            subfolder="levels",
        )

        assert url == DOWNLOADED_URL
        kwargs = download.await_args.kwargs
        assert kwargs["attachment_url"] == "https://cdn.discordapp.com/x.png"
        assert kwargs["file_extension"] == "png"
        assert kwargs["npc_name"] == "levels"

    async def test_entry_id_names_the_file(self, download):
        """No row id to use, so the submission guid has to be unique per file:
        download_player_image resolves collisions by scanning the directory."""
        await self._download(
            {"attachment_url": "https://cdn.discordapp.com/x.png"}, entry_id="guid-1"
        )
        assert download.await_args.kwargs["entry_id"] == "guid-1"

    async def test_api_transport_is_left_alone(self, download):
        assert await self._download(
            {"attachment_url": "https://cdn.discordapp.com/x.png", "downloaded": True}
        ) == ""
        download.assert_not_awaited()

    async def test_payload_without_an_attachment_is_a_no_op(self, download):
        assert await self._download({}) == ""
        download.assert_not_awaited()

    async def test_failed_download_answers_empty_not_none(self, monkeypatch):
        """Callers put the result straight into a payload; None would render."""
        from data.submissions import common

        monkeypatch.setattr(
            common, "download_player_image", AsyncMock(side_effect=RuntimeError("cdn 404"))
        )
        assert await self._download(
            {"attachment_url": "https://cdn.discordapp.com/x.png"}
        ) == ""

    async def test_download_returning_nothing_answers_empty(self, monkeypatch):
        from data.submissions import common

        monkeypatch.setattr(
            common, "download_player_image", AsyncMock(return_value=(None, None))
        )
        assert await self._download(
            {"attachment_url": "https://cdn.discordapp.com/x.png"}
        ) == ""


@pytest.fixture
def harness(monkeypatch):
    """Stub a processor's collaborators; capture its notifications."""
    from data.submissions import common

    monkeypatch.setattr(
        common,
        "download_player_image",
        AsyncMock(return_value=("/local/path.jpg", DOWNLOADED_URL)),
    )

    notifications = []

    def wire(module, session=None):
        player = _player()

        async def _create_notification(ntype, player_id, data, group_id=None,
                                       existing_session=None):
            notifications.append((ntype, data))
            return 1

        if hasattr(module, "ensure_can_create"):
            monkeypatch.setattr(module, "ensure_can_create", AsyncMock(return_value=True))
        monkeypatch.setattr(
            module,
            "ensure_player_by_name_then_auth",
            AsyncMock(return_value=(player, True, True)),
        )
        monkeypatch.setattr(
            module,
            "get_player_groups_with_global",
            lambda session, p: [SimpleNamespace(group_id=126, group_name="AstralStar")],
        )
        monkeypatch.setattr(module, "screenshot_required", AsyncMock(return_value=False))
        monkeypatch.setattr(module, "create_notification", _create_notification)
        monkeypatch.setattr(module, "is_user_dm_enabled", lambda *a, **k: False)
        if hasattr(module, "apply_account_type"):
            monkeypatch.setattr(module, "apply_account_type", lambda *a, **k: None)

        if session is not None:
            return session

        # The processor asks for group 2 when it is not already a member;
        # answering None keeps the loop to the one group above.
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = None
        return session

    # `from utils import group_config as gc` inside the loop: every group
    # has the notification type switched on.
    import utils

    gc = MagicMock()
    gc.get.return_value = "1"
    gc.is_truthy.return_value = True
    monkeypatch.setattr(utils, "group_config", gc, raising=False)

    return SimpleNamespace(wire=wire, notifications=notifications)


class TestProcessorsUseTheWebhookAttachment:
    """death / quest / diary must reach Discord with the image attached.

    Asserting on ``create_notification`` rather than on the stored row: the row
    having an ``image_url`` is not the bug anyone reported — the group's
    notification going out without a screenshot is.
    """

    @staticmethod
    def _payload(**extra):
        """A submission as bots/webhook_bot.py builds it: a CDN link, no file."""
        payload = {
            "player_name": "Mike Crouton",
            "acc_hash": "1234567890",
            "guid": "guid-1",
            "used_api": False,
            "attachment_url": "https://cdn.discordapp.com/attachments/1/2/image.png",
            "attachment_type": "image/png",
        }
        payload.update(extra)
        return payload

    async def test_death_notification_carries_the_screenshot(self, harness):
        from data.submissions import death

        session = harness.wire(death)

        await death.death_processor(
            self._payload(source="Yama"), external_session=session
        )

        group = [d for t, d in harness.notifications if t == "death"]
        assert group, "no death notification was queued"
        assert group[0]["image_url"] == DOWNLOADED_URL

    async def test_quest_notification_carries_the_screenshot(self, harness):
        from data.submissions import quest

        session = harness.wire(quest)

        await quest.quest_processor(
            self._payload(quest_name="Dragon Slayer II"), external_session=session
        )

        group = [d for t, d in harness.notifications if t == "quest"]
        assert group, "no quest notification was queued"
        assert group[0]["image_url"] == DOWNLOADED_URL

    async def test_diary_notification_carries_the_screenshot(self, harness):
        from data.submissions import diary

        session = harness.wire(diary)

        await diary.diary_processor(
            self._payload(diary_name="Ardougne", diary_tier="Elite"),
            external_session=session,
        )

        group = [d for t, d in harness.notifications if t == "diary"]
        assert group, "no diary notification was queued"
        assert group[0]["image_url"] == DOWNLOADED_URL

    async def test_api_transport_screenshot_is_not_re_downloaded(self, harness):
        """image_url already set = an API path saved the file; leave it alone."""
        from data.submissions import common, death

        session = harness.wire(death)
        already = "https://www.droptracker.io/img/user-upload/1234/death/Yama/Yama.jpg"

        await death.death_processor(
            self._payload(source="Yama", image_url=already, downloaded=True),
            external_session=session,
        )

        group = [d for t, d in harness.notifications if t == "death"]
        assert group[0]["image_url"] == already
        common.download_player_image.assert_not_awaited()

    async def test_death_without_a_screenshot_still_notifies(self, harness):
        """Screenshots off in the plugin: the death must still be announced."""
        from data.submissions import death

        session = harness.wire(death)
        payload = self._payload(source="Yama")
        payload.pop("attachment_url")
        payload.pop("attachment_type")

        await death.death_processor(payload, external_session=session)

        group = [d for t, d in harness.notifications if t == "death"]
        assert group, "a death with no screenshot must still be announced"
        assert group[0]["image_url"] == ""


class TestExperienceNotificationsCarryTheScreenshot:
    """Level-ups and XP milestones, which have no row to hang the image on.

    ``experience.py`` builds two independent notification payloads on two
    independent code paths, and only one of them runs per submission, so both
    are covered here. The plugin screenshots level-ups when they clear
    ``minLevelToScreenshot`` and screenshots XP milestones unconditionally, so
    on the webhook transport both arrived with an ``attachment_url`` that the
    processor never read.
    """

    @staticmethod
    def _level_up(**extra):
        """A level-up as bots/webhook_bot.py builds it: a CDN link, no file."""
        payload = {
            "player_name": "Mike Crouton",
            "acc_hash": "1234567890",
            "guid": "guid-1",
            "used_api": False,
            "attachment_url": "https://cdn.discordapp.com/attachments/1/2/image.png",
            "attachment_type": "image/png",
            "skills_leveled": "Attack",
            "attack_new_level": 70,
            "attack_level_gained": 1,
            "attack_xp_total": 737_627,
            "total_level": 1500,
            "combat_level": 90,
        }
        payload.update(extra)
        return payload

    @staticmethod
    def _milestone(**extra):
        payload = {
            "player_name": "Mike Crouton",
            "acc_hash": "1234567890",
            "guid": "guid-2",
            "used_api": False,
            "type": "experience_milestone",
            "attachment_url": "https://cdn.discordapp.com/attachments/1/2/image.png",
            "attachment_type": "image/png",
            "skills_trained": "Attack",
            "attack_xp_milestone": 50_000_000,
            "attack_xp_total": 50_000_412,
            "xp_milestone_interval": 1_000_000,
            "total_level": 2277,
            "combat_level": 126,
        }
        payload.update(extra)
        return payload

    async def test_level_up_notification_carries_the_screenshot(self, harness):
        from data.submissions import experience

        session = harness.wire(experience, session=_experience_session())

        await experience.experience_processor(self._level_up(), external_session=session)

        group = [d for t, d in harness.notifications if t == "level_up"]
        assert group, "no level_up notification was queued"
        assert group[0]["image_url"] == DOWNLOADED_URL

    async def test_xp_milestone_notification_carries_the_screenshot(self, harness):
        from data.submissions import experience

        session = harness.wire(experience, session=_experience_session())

        await experience.experience_processor(self._milestone(), external_session=session)

        group = [d for t, d in harness.notifications if t == "xp_milestone"]
        assert group, "no xp_milestone notification was queued"
        assert group[0]["image_url"] == DOWNLOADED_URL

    async def test_the_file_is_named_from_the_submission_guid(self, harness):
        """There is no row id: PlayerExperience is a per-player XP snapshot."""
        from data.submissions import common, experience

        session = harness.wire(experience, session=_experience_session())

        await experience.experience_processor(self._level_up(), external_session=session)

        kwargs = common.download_player_image.await_args.kwargs
        assert kwargs["entry_id"] == "guid-1"
        assert kwargs["submission_type"] == "experience"

    async def test_a_group_requiring_screenshots_still_gets_the_level_up(
        self, harness, monkeypatch
    ):
        """The gate reads the resolved URL, so resolution must precede it.

        Resolving after the loop would not merely cost the image — it would
        cost the announcement, for exactly the groups that care most about it.
        """
        from data.submissions import experience

        session = harness.wire(experience, session=_experience_session())
        monkeypatch.setattr(
            experience, "screenshot_required", AsyncMock(return_value=True)
        )

        await experience.experience_processor(self._level_up(), external_session=session)

        group = [d for t, d in harness.notifications if t == "level_up"]
        assert group, "the screenshot was resolved too late for the gate to see it"
        assert group[0]["image_url"] == DOWNLOADED_URL

    async def test_a_group_requiring_screenshots_still_skips_one_without(
        self, harness, monkeypatch
    ):
        """The gate itself has to keep working."""
        from data.submissions import experience

        session = harness.wire(experience, session=_experience_session())
        monkeypatch.setattr(
            experience, "screenshot_required", AsyncMock(return_value=True)
        )
        payload = self._level_up()
        payload.pop("attachment_url")
        payload.pop("attachment_type")

        await experience.experience_processor(payload, external_session=session)

        assert not [d for t, d in harness.notifications if t == "level_up"]

    async def test_api_transport_screenshot_is_not_re_downloaded(self, harness):
        from data.submissions import common, experience

        session = harness.wire(experience, session=_experience_session())
        already = "https://www.droptracker.io/img/user-upload/1234/experience/levels/Attack.jpg"

        await experience.experience_processor(
            self._level_up(image_url=already, downloaded=True), external_session=session
        )

        group = [d for t, d in harness.notifications if t == "level_up"]
        assert group[0]["image_url"] == already
        common.download_player_image.assert_not_awaited()

    async def test_level_up_without_a_screenshot_still_notifies(self, harness):
        from data.submissions import experience

        session = harness.wire(experience, session=_experience_session())
        payload = self._level_up()
        payload.pop("attachment_url")
        payload.pop("attachment_type")

        await experience.experience_processor(payload, external_session=session)

        group = [d for t, d in harness.notifications if t == "level_up"]
        assert group, "a level-up with no screenshot must still be announced"
        assert group[0]["image_url"] == ""


# Helpers that resolve the webhook attachment on a processor's behalf.
_ATTACHMENT_HELPERS = {
    "attach_webhook_screenshot",
    "download_webhook_screenshot",
    "_resolve_webhook_screenshot",
    "resolve_attachment_from_drop_data",
}


def _reads_webhook_attachment(path: Path) -> bool:
    """Whether a processor consumes ``attachment_url``, directly or via a helper.

    Deliberately not a substring search: a comment mentioning the key would
    satisfy that while the code still throws the screenshot away.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "attachment_url":
            return True  # data.get("attachment_url")
        if isinstance(node, ast.Name) and node.id == "attachment_url":
            return True  # attachment_url, _ = resolve_attachment_from_drop_data(...)
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in _ATTACHMENT_HELPERS:
                return True
    return False


class TestEveryImageBearingProcessorReadsTheAttachment:
    """Static guard: the omission that caused this must not recur.

    Every processor whose submission can carry a screenshot has to consume
    ``attachment_url``, or the Discord-webhook transport silently loses it. This
    is a source-level check on purpose — the failure mode has no exception, no
    log line and no failed row to assert on, so only the absence of the code is
    observable.
    """

    # Types the plugin captures a screenshot for (SubmissionManager's switch).
    # All but `experience` persist the URL on an image_url column; experience
    # has no per-event row and passes it straight to the notification payload.
    IMAGE_BEARING = {
        "ca": "combat achievements",
        "clog": "collection log slots",
        "death": "deaths",
        "diary": "achievement diaries",
        "drop": "drops",
        "experience": "level-ups and XP milestones",
        "pb": "personal bests",
        "pet": "pets",
        "quest": "quest completions",
    }

    @pytest.mark.parametrize("module_name", sorted(IMAGE_BEARING))
    def test_processor_reads_attachment_url(self, module_name):
        path = PROCESSOR_DIR / f"{module_name}.py"
        assert path.exists(), f"{path} is missing — did the module get renamed?"
        assert _reads_webhook_attachment(path), (
            f"data/submissions/{module_name}.py never reads 'attachment_url', so every "
            f"screenshot for {self.IMAGE_BEARING[module_name]} submitted through the "
            f"Discord-webhook transport (the plugin's default: useApi=false) is "
            f"silently discarded. Call attach_webhook_screenshot() after the row is "
            f"flushed and before the notification loop."
        )

    # The four that lost screenshots for months, and the helper each one calls.
    REGRESSED = {
        "death": "attach_webhook_screenshot(",
        "quest": "attach_webhook_screenshot(",
        "diary": "attach_webhook_screenshot(",
        "experience": "_resolve_webhook_screenshot(",
    }

    @pytest.mark.parametrize("module_name", sorted(REGRESSED))
    def test_the_ones_that_regressed_use_a_shared_helper(self, module_name):
        source = (PROCESSOR_DIR / f"{module_name}.py").read_text()
        assert self.REGRESSED[module_name] in source, (
            f"{module_name}.py no longer calls {self.REGRESSED[module_name]}"
        )

    @pytest.mark.parametrize("module_name", sorted(REGRESSED))
    def test_helper_runs_before_the_screenshot_gate(self, module_name):
        """Resolving late costs the announcement, not just the image.

        ``screenshot_required`` groups drop a submission that reaches the loop
        with no image, so an image resolved after that gate is worse than
        useless: the group never hears about the event at all.
        """
        source = (PROCESSOR_DIR / f"{module_name}.py").read_text()
        resolve_at = source.index(self.REGRESSED[module_name])
        assert resolve_at < source.index("screenshot_required("), (
            f"{module_name}.py resolves its screenshot after the "
            f"screenshot_required() gate, which drops the notification entirely"
        )
        assert resolve_at < source.index("create_notification("), (
            f"{module_name}.py resolves its screenshot after the notification is "
            f"built, so the group's Discord message still goes out without it"
        )
