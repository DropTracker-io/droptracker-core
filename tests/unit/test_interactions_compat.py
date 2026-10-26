"""utils/interactions_compat.py: an enum value Discord added before the
library did must not crash gateway event parsing ('components' embeds)."""

from enum import Enum

from utils.interactions_compat import _tolerant_missing


class _Kind(Enum):
    RICH = "rich"


_Kind._missing_ = classmethod(_tolerant_missing)


def test_unknown_string_becomes_a_stand_in_member():
    member = _Kind("components")
    assert isinstance(member, _Kind)
    assert member.value == "components"
    assert member.name == "UNKNOWN_COMPONENTS"


def test_known_values_are_unchanged():
    assert _Kind("rich") is _Kind.RICH


def test_non_strings_are_still_refused():
    try:
        _Kind(5)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
