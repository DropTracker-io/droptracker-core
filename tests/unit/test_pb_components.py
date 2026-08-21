"""Layout rules for the Components V2 personal best message."""
from services.pb_components import IS_COMPONENTS_V2, build_pb_message

BASE = dict(player_name="Ra ine", boss="Vorkath", time_display="1:52.20")


def container(msg):
    return msg["components"][0]


def texts(msg):
    """Every text body in the message, flattened."""
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == 10:
                out.append(node["content"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(msg)
    return out


def test_carries_the_v2_flag_and_no_embeds():
    """A V2 message may not also carry content or embeds — Discord rejects it."""
    msg = build_pb_message(**BASE)
    assert msg["flags"] == IS_COMPONENTS_V2
    assert "content" not in msg
    assert "embeds" not in msg


def test_character_becomes_the_headline_accessory():
    msg = build_pb_message(**BASE, character_image_url="https://example/c.png")
    section = next(c for c in container(msg)["components"] if c.get("type") == 9)
    assert section["accessory"]["media"]["url"] == "https://example/c.png"


def test_degrades_without_a_character():
    """Most players have not uploaded a model; the section must still render."""
    msg = build_pb_message(**BASE)
    section = next(c for c in container(msg)["components"] if c.get("type") == 9)
    assert "accessory" not in section
    assert any("Vorkath" in t for t in texts(msg))


def test_screenshot_stays_full_width_not_a_thumbnail():
    """The player's own capture is the evidence; shrinking it defeats the point."""
    msg = build_pb_message(**BASE, screenshot_url="https://example/s.png")
    gallery = next(c for c in container(msg)["components"] if c.get("type") == 12)
    assert gallery["items"][0]["media"]["url"] == "https://example/s.png"


def test_previous_best_is_shown_when_known():
    msg = build_pb_message(**BASE, previous_best="2:04.80")
    assert any("2:04.80" in t for t in texts(msg))


def test_omits_stats_that_are_unknown():
    """A group with points disabled should not get an empty "Points" line."""
    msg = build_pb_message(**BASE, group_rank=3, group_total=48)
    body = "\n".join(texts(msg))
    assert "Group rank" in body
    assert "Points" not in body
    assert "Global rank" not in body


def test_note_is_rendered_as_small_print():
    msg = build_pb_message(**BASE, note="Pending review")
    assert any(t.startswith("-# Pending review") for t in texts(msg))


def test_ranks_are_formatted_with_totals():
    msg = build_pb_message(**BASE, global_rank=214, global_total=12905)
    assert any("#214 of 12,905" in t for t in texts(msg))
