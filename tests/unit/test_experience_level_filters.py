"""Unit tests for per-group, per-skill level-notification filtering.

Covers the rework that made "only announce 99s" configurable: per-skill
qualification (no more tag-along skills), the notify_virtual_levels and
notify_combat_levels opt-ins, and crossed-level checks on multi-level jumps.
"""

from data.submissions.experience import _skill_qualifies_for_group


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
