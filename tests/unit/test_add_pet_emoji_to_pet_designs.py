"""The pet-design rollout puts {pet_emoji} beside {pet_name}, once, and undoes it."""
from scripts.add_pet_emoji_to_pet_designs import rewritten_embed, rewritten_layout


class TestEmbed:
    def test_token_goes_beside_the_name(self):
        assert rewritten_embed("{player_name} is being followed by {pet_name}", None, False) == {
            "title": "{player_name} is being followed by {pet_emoji} {pet_name}"}

    def test_description_when_the_title_lacks_the_name(self):
        assert rewritten_embed("{player_name}", "**{pet_name}** van {source}", False) == {
            "description": "**{pet_emoji} {pet_name}** van {source}"}

    def test_title_wins_over_description(self):
        assert rewritten_embed("{pet_name}", "Got {pet_name}", False) == {
            "title": "{pet_emoji} {pet_name}"}

    def test_already_has_the_token_anywhere(self):
        assert rewritten_embed("{pet_name}", "{pet_emoji}", False) == {}

    def test_no_name_no_change(self):
        assert rewritten_embed("New pet!", "Congrats", False) == {}

    def test_over_long_title_is_left_alone(self):
        assert rewritten_embed("{pet_name}" + "x" * 250, None, False) == {}

    def test_revert_round_trips(self):
        added = rewritten_embed("{player_name} received {pet_name}!", None, False)
        assert rewritten_embed(added["title"], None, True) == {
            "title": "{player_name} received {pet_name}!"}


class TestLayout:
    LAYOUT = {"accent_color": "#c8aa6e", "blocks": [
        {"type": "text", "content": "**{player_name}** got a pet"},
        {"type": "separator", "divider": True},
        {"type": "text", "content": "## {pet_name}\nReceived at `{killcount}`"},
        {"type": "section", "content": "**{pet_name}**", "thumbnail": "{image_url}"},
    ]}

    def test_every_block_naming_the_pet(self):
        out = rewritten_layout(self.LAYOUT, False)
        assert out["blocks"][2]["content"] == "## {pet_emoji} {pet_name}\nReceived at `{killcount}`"
        assert out["blocks"][3] == {"type": "section", "content": "**{pet_emoji} {pet_name}**",
                                    "thumbnail": "{image_url}"}
        assert out["blocks"][:2] == self.LAYOUT["blocks"][:2]
        assert out["accent_color"] == "#c8aa6e"

    def test_idempotent(self):
        assert rewritten_layout(rewritten_layout(self.LAYOUT, False), False) is None

    def test_no_name_no_change(self):
        assert rewritten_layout({"blocks": [{"type": "text", "content": "hi"}]}, False) is None

    def test_revert_round_trips(self):
        assert rewritten_layout(rewritten_layout(self.LAYOUT, False), True) == self.LAYOUT
