"""Unit tests for per-group, per-skill level-notification filtering.

Covers the rework that made "only announce 99s" configurable: per-skill
qualification (no more tag-along skills), the notify_virtual_levels and
notify_combat_levels opt-ins, and crossed-level checks on multi-level jumps.

Also covers the total-level milestone path, which rendered with the level_up
embed template and so leaked virtual levels into groups that had opted out:
milestone-list resolution, the crossing (not membership) test, and the
opt-in-only skill visibility its context line uses.
"""

from data.submissions.experience import (
    _crosses_total_milestone,
    _milestone_levels_for_group,
    _skill_qualifies_for_group,
    _skill_visible_to_group,
)


def _skill(key, level, gained=1):
    return {
        "skill_key": key,
        "skill_name": key.title(),
        "new_level": level,
        "levels_gained": gained,
    }


DEFAULTS = dict(
    minimum_level=1,
    level_increment=1,
    notify_virtual_levels=False,
    notify_combat_levels=False,
)


def qualifies(skill, **overrides):
    return _skill_qualifies_for_group(skill, **{**DEFAULTS, **overrides})


VISIBLE_DEFAULTS = dict(notify_virtual_levels=False, notify_combat_levels=False)


def visible(skill, **overrides):
    return _skill_visible_to_group(skill, **{**VISIBLE_DEFAULTS, **overrides})


class TestOnly99sConfig:
    """The reported use case: min=99, increment=99, only 99s should notify."""

    CFG = dict(minimum_level=99, level_increment=99)

    def test_99_qualifies(self):
        assert qualifies(_skill("attack", 99), **self.CFG)

    def test_pre_99_does_not(self):
        assert not qualifies(_skill("herblore", 76), **self.CFG)
        assert not qualifies(_skill("attack", 98), **self.CFG)

    def test_virtual_levels_do_not(self):
        assert not qualifies(_skill("magic", 100), **self.CFG)
        assert not qualifies(_skill("magic", 126), **self.CFG)

    def test_combat_does_not(self):
        # Combat 99 previously slipped through the "level 99 always
        # notifies" special case.
        assert not qualifies(_skill("combat", 99), **self.CFG)
        assert not qualifies(_skill("combat", 105), **self.CFG)


class TestDefaults:
    def test_every_real_level_qualifies(self):
        assert qualifies(_skill("attack", 2))
        assert qualifies(_skill("attack", 50))
        assert qualifies(_skill("attack", 99))

    def test_virtual_levels_off_by_default(self):
        assert not qualifies(_skill("magic", 100))

    def test_combat_off_by_default(self):
        assert not qualifies(_skill("combat", 50))

    def test_invalid_level_never_qualifies(self):
        assert not qualifies(_skill("attack", 0))
        assert not qualifies({"skill_key": "attack"})


class TestVirtualLevels:
    def test_opt_in_enables_virtual(self):
        assert qualifies(_skill("magic", 100), notify_virtual_levels=True)
        assert qualifies(_skill("magic", 126), notify_virtual_levels=True)

    def test_increment_applies_to_virtual(self):
        assert qualifies(_skill("magic", 110), notify_virtual_levels=True, level_increment=10)
        assert not qualifies(_skill("magic", 101), notify_virtual_levels=True, level_increment=10)

    def test_minimum_still_applies_below_99(self):
        assert not qualifies(_skill("magic", 50), notify_virtual_levels=True, minimum_level=90)


class TestCombatLevels:
    def test_opt_in_ignores_min_and_increment(self):
        cfg = dict(notify_combat_levels=True, minimum_level=99, level_increment=99)
        assert qualifies(_skill("combat", 50), **cfg)
        assert qualifies(_skill("combat", 126), **cfg)


class TestIncrementAndCrossedLevels:
    def test_increment_alignment(self):
        assert qualifies(_skill("attack", 60), level_increment=10)
        assert not qualifies(_skill("attack", 61), level_increment=10)

    def test_99_always_notifies_despite_increment(self):
        assert qualifies(_skill("attack", 99), level_increment=10)

    def test_multi_level_jump_crossing_aligned_level(self):
        # 59 -> 61 crosses 60; the final level alone (61) is misaligned.
        assert qualifies(_skill("attack", 61, gained=2), level_increment=10)

    def test_lamp_over_99_still_announces_the_99(self):
        # 98 -> 100 crosses 99 even with virtual levels disabled.
        assert qualifies(_skill("magic", 100, gained=2))
        assert qualifies(_skill("magic", 100, gained=2), minimum_level=99, level_increment=99)

    def test_crossed_virtual_levels_ignored_when_disabled(self):
        # 100 -> 102: only virtual levels crossed, virtual disabled.
        assert not qualifies(_skill("magic", 102, gained=2))


class TestSkillVisibleToGroup:
    """`_skill_visible_to_group` — the opt-in half, used for milestone context.

    Total-level milestones are their own event, so the min/increment filters
    must NOT apply; the virtual/combat opt-ins still must.
    """

    def test_real_levels_always_visible(self):
        assert visible(_skill("attack", 1))
        assert visible(_skill("attack", 99))

    def test_virtual_hidden_unless_opted_in(self):
        assert not visible(_skill("magic", 100))
        assert not visible(_skill("magic", 126))
        assert visible(_skill("magic", 100), notify_virtual_levels=True)

    def test_combat_hidden_unless_opted_in(self):
        assert not visible(_skill("combat", 105))
        assert visible(_skill("combat", 105), notify_combat_levels=True)

    def test_minimum_and_increment_do_not_apply(self):
        # A skill that would never earn its own level-up announcement still
        # belongs on a milestone's context line.
        assert visible(_skill("attack", 3))

    def test_invalid_level_never_visible(self):
        assert not visible(_skill("attack", 0))
        assert not visible({"skill_key": "attack"})


class TestMilestoneLevelsForGroup:
    """Canonical `level_milestones` vs the registry-invisible legacy key."""

    LEGACY = "[1500,1750,2000,2050,2100,2150,2200,2277,2376]"

    def test_legacy_used_when_canonical_row_absent(self):
        assert _milestone_levels_for_group(
            {"level_milestones_to_notify": self.LEGACY}
        ) == [1500, 1750, 2000, 2050, 2100, 2150, 2200, 2277, 2376]

    def test_canonical_wins_when_present(self):
        assert _milestone_levels_for_group(
            {"level_milestones": "2000,2277", "level_milestones_to_notify": self.LEGACY}
        ) == [2000, 2277]

    def test_cleared_canonical_disables_milestones(self):
        # Group 315, 2026-08-27: the admin cleared the list in the editor and
        # kept getting milestone posts, because "" fell back to the legacy key.
        assert _milestone_levels_for_group(
            {"level_milestones": "", "level_milestones_to_notify": self.LEGACY}
        ) == []

    def test_no_rows_at_all(self):
        assert _milestone_levels_for_group({}) == []


class TestCrossesTotalMilestone:
    MILESTONES = [1500, 2000, 2277, 2376]

    def test_crossing_fires(self):
        assert _crosses_total_milestone(1999, 2000, self.MILESTONES)

    def test_multi_level_jump_over_a_milestone_fires(self):
        assert _crosses_total_milestone(1498, 1501, self.MILESTONES)

    def test_sitting_on_a_milestone_does_not_refire(self):
        # The bug: a maxed player parks on 2376, so every later level-up (all
        # of them virtual) re-announced the milestone.
        assert not _crosses_total_milestone(2376, 2376, self.MILESTONES)

    def test_unchanged_total_never_fires(self):
        assert not _crosses_total_milestone(2000, 2000, self.MILESTONES)

    def test_no_baseline_does_not_fire(self):
        assert not _crosses_total_milestone(0, 2000, self.MILESTONES)

    def test_gain_between_milestones_does_not_fire(self):
        assert not _crosses_total_milestone(2001, 2100, self.MILESTONES)

    def test_empty_milestone_list_never_fires(self):
        assert not _crosses_total_milestone(1999, 2000, [])
