"""Recap delivery timing and audience rules (services/recap_delivery.py).

The pure half — who is owed a card and when it may be sent. Both halves of that
have a failure mode worth a test rather than a comment:

* a send scheduled before its month closes produces a card for a month that is
  still running, which is the one mistake a recap cannot walk back;
* an entitlement rule that drifts either spams people who never asked, or
  silently denies someone the one card they were owed.

Loaded from the file path so the conftest ``services`` stub doesn't shadow it.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "recap_delivery.py",
)
_spec = importlib.util.spec_from_file_location("_recap_delivery_under_test", _MODULE_PATH)
delivery = importlib.util.module_from_spec(_spec)
sys.modules["_recap_delivery_under_test"] = delivery
_spec.loader.exec_module(delivery)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestPeriodSelection:
    def test_january_run_sends_decembers_card(self):
        assert delivery.last_completed_month(_utc(2027, 1, 1, 0, 30)) == "2026-12"

    def test_mid_month_run_still_targets_last_month(self):
        assert delivery.last_completed_month(_utc(2026, 7, 31, 12)) == "2026-06"

    def test_month_close_is_midnight_utc_on_the_first(self):
        assert delivery.month_close_utc("2026-06") == _utc(2026, 7, 1)

    def test_december_closes_into_the_new_year(self):
        assert delivery.month_close_utc("2026-12") == _utc(2027, 1, 1)


class TestDueTime:
    def test_utc_group_at_midnight_sends_at_the_close(self):
        assert delivery.due_at_utc("2026-06", "UTC", 0) == _utc(2026, 7, 1)

    def test_no_timezone_is_treated_as_utc(self):
        assert delivery.due_at_utc("2026-06", None, 9) == _utc(2026, 7, 1, 9)

    def test_zone_behind_utc_sends_later_in_utc_terms(self):
        # 09:00 in New York on 1 July is 13:00 UTC (EDT).
        assert delivery.due_at_utc("2026-06", "America/New_York", 9) == _utc(2026, 7, 1, 13)

    def test_zone_ahead_of_utc_is_clamped_to_the_month_close(self):
        # Auckland's local midnight on the 1st is 11:00 UTC on the 31st — a
        # moment when June has not finished and the card cannot be built.
        due = delivery.due_at_utc("2026-06", "Pacific/Auckland", 0)
        assert due == _utc(2026, 7, 1)
        assert due >= delivery.month_close_utc("2026-06")

    def test_zone_ahead_of_utc_keeps_a_later_hour(self):
        # 18:00 in Auckland is 06:00 UTC — after the close, so it stands.
        assert delivery.due_at_utc("2026-06", "Pacific/Auckland", 18) == _utc(2026, 7, 1, 6)

    def test_unknown_zone_falls_back_to_utc_rather_than_raising(self):
        assert delivery.due_at_utc("2026-06", "Mars/Olympus_Mons", 3) == _utc(2026, 7, 1, 3)

    def test_hour_is_clamped_into_range(self):
        assert delivery.due_at_utc("2026-06", "UTC", 99) == _utc(2026, 7, 1, 23)
        assert delivery.due_at_utc("2026-06", "UTC", -5) == _utc(2026, 7, 1)


class TestIsDue:
    def test_not_due_before_the_hour(self):
        assert not delivery.is_due(_utc(2026, 7, 1, 8), "2026-06", "UTC", 9)

    def test_due_at_the_hour(self):
        assert delivery.is_due(_utc(2026, 7, 1, 9), "2026-06", "UTC", 9)

    def test_still_due_inside_the_catch_up_window(self):
        # The bot being down on the 1st shouldn't cost a clan its card.
        assert delivery.is_due(_utc(2026, 7, 3, 9), "2026-06", "UTC", 9)

    def test_not_due_once_the_window_has_passed(self):
        # A "recap" arriving on the 20th is a surprise, not a recap.
        assert not delivery.is_due(_utc(2026, 7, 20, 9), "2026-06", "UTC", 9)

    def test_naive_now_is_assumed_utc(self):
        naive = datetime(2026, 7, 1, 9)
        assert delivery.is_due(naive, "2026-06", "UTC", 9)

    def test_grace_window_is_configurable(self):
        late = _utc(2026, 7, 1, 9) + timedelta(days=5)
        assert not delivery.is_due(late, "2026-06", "UTC", 9)
        assert delivery.is_due(late, "2026-06", "UTC", 9, grace_days=7)


class TestBestAccount:
    def test_picks_the_biggest_month(self):
        assert delivery.pick_best_account([(1, 50), (2, 900), (3, 100)]) == 2

    def test_ties_break_on_the_lower_id_so_reruns_agree(self):
        assert delivery.pick_best_account([(9, 100), (4, 100)]) == 4

    def test_zero_loot_accounts_are_still_candidates(self):
        # Someone whose only tracked account had a quiet month still gets a card.
        assert delivery.pick_best_account([(7, 0)]) == 7

    def test_no_accounts_means_no_card(self):
        assert delivery.pick_best_account([]) is None

    def test_none_loot_is_treated_as_zero(self):
        assert delivery.pick_best_account([(1, None), (2, 5)]) == 2


class TestEntitlement:
    def test_first_card_is_unsolicited(self):
        assert delivery.user_is_entitled(opted_in=False, had_prior=False)

    def test_second_card_requires_opting_in(self):
        assert not delivery.user_is_entitled(opted_in=False, had_prior=True)

    def test_opted_in_users_keep_receiving(self):
        assert delivery.user_is_entitled(opted_in=True, had_prior=True)

    def test_opting_in_before_the_first_card_is_harmless(self):
        assert delivery.user_is_entitled(opted_in=True, had_prior=False)


class TestMessages:
    def _target(self, **kw):
        base = dict(
            user_id=1, discord_id="123", player_id=5,
            player_name="Buzzyn", period="2026-06",
        )
        base.update(kw)
        return delivery.UserTarget(**base)

    def test_first_card_offers_both_choices(self):
        msg = delivery.build_dm_message(self._target(opted_in=False), {}, None)
        labels = [c["label"] for c in msg["components"][0]["components"]]
        assert "Keep sending these" in labels and "No thanks" in labels

    def test_repeat_card_only_offers_the_way_out(self):
        # Nobody should have to re-confirm a choice they already made.
        msg = delivery.build_dm_message(self._target(opted_in=True), {}, None)
        labels = [c["label"] for c in msg["components"][0]["components"]]
        assert "Stop sending these" in labels
        assert "Keep sending these" not in labels

    def test_first_card_explains_why_it_arrived(self):
        msg = delivery.build_dm_message(self._target(opted_in=False), {}, None)
        assert "first monthly recap" in (msg["content"] or "")

    def test_repeat_card_says_nothing_extra(self):
        assert delivery.build_dm_message(self._target(opted_in=True), {}, None)["content"] is None

    def test_image_is_attached_when_rendered(self):
        msg = delivery.build_dm_message(self._target(), {}, "https://img/card.png")
        assert msg["embeds"][0]["image"]["url"] == "https://img/card.png"

    def test_missing_image_still_sends_a_card(self):
        # A render failure costs the picture, not the message.
        msg = delivery.build_dm_message(self._target(), {}, None)
        assert "image" not in msg["embeds"][0]

    def test_summary_omits_figures_the_payload_lacks(self):
        # Same omit-never-zero rule the card itself follows.
        line = delivery._summary_line({"totals": {"loot": 1_000_000_000}})
        assert "1.00B" in line and "drops" not in line

    def test_summary_is_empty_for_an_empty_payload(self):
        assert delivery._summary_line({}) == ""

    def test_group_card_carries_no_opt_in_buttons(self):
        # A channel post isn't one person's to decide.
        target = delivery.GroupTarget(group_id=14, name="Pegasus PvM", channel_id="1", period="2026-06")
        msg = delivery.build_channel_message(target, {}, None)
        ids = [c.get("custom_id") for c in msg["components"][0]["components"]]
        assert ids == [None]

    def test_test_banner_names_the_real_recipient(self):
        msg = delivery.build_dm_message(self._target(opted_in=True), {}, None)
        banner = delivery.with_test_banner(msg, "someone else")
        assert "Test" in banner["content"] and "someone else" in banner["content"]

    def test_test_banner_keeps_the_original_content(self):
        msg = delivery.build_dm_message(self._target(opted_in=False), {}, None)
        banner = delivery.with_test_banner(msg, "someone else")
        assert "first monthly recap" in banner["content"]


class TestFlags:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv(delivery.ENV_ENABLED, raising=False)
        assert not delivery.delivery_enabled()

    def test_enabled_by_truthy_values(self, monkeypatch):
        for value in ("true", "1", "yes", "on", "TRUE"):
            monkeypatch.setenv(delivery.ENV_ENABLED, value)
            assert delivery.delivery_enabled()

    def test_blank_test_target_is_no_test_target(self, monkeypatch):
        monkeypatch.setenv(delivery.ENV_TEST_TARGET, "   ")
        assert delivery.test_target() is None
