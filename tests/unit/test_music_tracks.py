"""Suggestion #252: decoding music unlock varps."""
from utils.music_tracks import decode

TRACKS = [
    {"id": 1, "varp": 20, "bit": 0},
    {"id": 2, "varp": 20, "bit": 31},
    {"id": 3, "varp": 21, "bit": 3},
    {"id": 4, "varp": 99, "bit": 0, "automatic": True},
    {"id": 5, "varp": 20, "bit": 5, "hidden": True},
]


def test_bits_automatic_and_hidden():
    # bit 31 set arrives as a negative signed int from the client
    result = decode({20: -2147483647, 21: 0b1000}, TRACKS)
    assert result["unlocked_ids"] == [1, 2, 3, 4]
    assert result["total"] == 4


def test_missing_varp_is_not_unlocked():
    result = decode({20: 1}, TRACKS)
    assert result["unlocked_ids"] == [1, 4]
