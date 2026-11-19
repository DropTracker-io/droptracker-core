"""The advertised plugin version follows the Plugin Hub, and only a real release."""
import pytest

from utils import plugin_release as pr

PIN = """repository=https://github.com/joelhalen/droptracker-plugin.git
commit=af7b172db2238b1d6ffc54dc293189bbf287fc15
authors=joelhalen,OSRSKoeppy
"""
GRADLE = """plugins { id 'java' }
def runeLiteVersion = 'latest.release'
group = 'io.droptracker'
version = '6.1.29'
"""


class TestParsing:
    def test_pin(self):
        assert pr.parse_pin(PIN) == ("joelhalen/droptracker-plugin",
                                     "af7b172db2238b1d6ffc54dc293189bbf287fc15")

    @pytest.mark.parametrize("text", [
        "", None, "repository=https://github.com/a/b.git\ncommit=abc123\n",          # short sha
        "commit=af7b172db2238b1d6ffc54dc293189bbf287fc15\n",                         # no repository
        "repository=https://example.com/a/b.git\ncommit=af7b172db2238b1d6ffc54dc293189bbf287fc15\n",
        "404: Not Found",
    ])
    def test_a_malformed_pin_is_nothing(self, text):
        assert pr.parse_pin(text) is None

    def test_gradle_version(self):
        assert pr.parse_gradle_version(GRADLE) == "6.1.29"
        assert pr.parse_gradle_version('version = "6.2"') == "6.2"
        # Not the RuneLite version line, and not prose.
        assert pr.parse_gradle_version("def runeLiteVersion = 'latest.release'") is None
        assert pr.parse_gradle_version("404: Not Found") is None


class TestDecision:
    def test_approved_and_in_players_hands_is_advertised(self):
        new, why = pr.decide("6.1.20", "6.1.27", 4652, 50)
        assert new == "6.1.27" and "4652 players" in why

    def test_approved_but_not_yet_released_waits(self):
        new, why = pr.decide("6.1.27", "6.1.29", 12, 50)
        assert new is None and "not yet released" in why

    def test_never_downgrades_or_repeats(self):
        assert pr.decide("6.1.29", "6.1.27", 9999, 50)[0] is None
        assert pr.decide("6.1.29", "6.1.29", 9999, 50)[0] is None

    def test_versions_compare_as_numbers(self):
        assert pr.decide("6.1.9", "6.1.10", 100, 50)[0] == "6.1.10"
        assert pr.decide("6.1.10", "6.1.9", 100, 50)[0] is None

    def test_an_unreadable_hub_version_changes_nothing(self):
        assert pr.decide("6.1.20", None, 9999, 50)[0] is None
        assert pr.decide("6.1.20", "banana", 9999, 50)[0] is None

    def test_nothing_advertised_yet(self):
        assert pr.decide(None, "6.1.27", 100, 50)[0] == "6.1.27"


class TestPinnedVersion:
    def _fetch(self, pin=PIN, gradle=GRADLE):
        seen = []

        def fetch(url):
            seen.append(url)
            return pin if url == pr.HUB_PIN_URL else gradle

        return fetch, seen

    def test_reads_the_version_at_the_pinned_commit(self):
        fetch, seen = self._fetch()
        assert pr.pinned_version(fetch) == ("6.1.29", "af7b172db2238b1d6ffc54dc293189bbf287fc15")
        assert seen[1] == ("https://raw.githubusercontent.com/joelhalen/droptracker-plugin/"
                           "af7b172db2238b1d6ffc54dc293189bbf287fc15/build.gradle")

    def test_a_pin_to_another_repository_is_never_followed(self):
        other = PIN.replace("joelhalen/droptracker-plugin", "someone/else")
        fetch, seen = self._fetch(pin=other)
        assert pr.pinned_version(fetch) == (None, None) and len(seen) == 1


class _Row:
    def __init__(self, value):
        self.config_value = value


class _Session:
    """Just enough of a session for `sync`."""

    def __init__(self, rows, players):
        self.rows, self.players, self.added, self.flushed = rows, players, [], False

    def query(self, _model):
        return self

    def filter(self, *_a):
        return self

    def all(self):
        return self.rows

    def execute(self, *_a, **_k):
        return self

    def scalar(self):
        return self.players

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flushed = True


def _fetch(url):
    return PIN if url == pr.HUB_PIN_URL else GRADLE


class TestSync:
    def test_a_release_moves_the_row(self):
        row = _Row("6.1.20")
        session = _Session([row], players=800)
        out = pr.sync(session, apply=True, fetch=_fetch, min_players=50)
        assert out["changed"] and row.config_value == "6.1.29" and session.flushed
        assert (out["advertised"], out["pinned"], out["players"]) == ("6.1.20", "6.1.29", 800)

    def test_a_dry_run_only_says_what_it_would_do(self):
        row = _Row("6.1.20")
        out = pr.sync(_Session([row], players=800), apply=False, fetch=_fetch, min_players=50)
        assert not out["changed"] and out["would_change"] and row.config_value == "6.1.20"

    def test_too_few_players_leaves_it_alone(self):
        row = _Row("6.1.20")
        out = pr.sync(_Session([row], players=3), apply=True, fetch=_fetch, min_players=50)
        assert not out["changed"] and row.config_value == "6.1.20"

    def test_the_last_filled_row_is_the_one_the_endpoint_serves(self):
        first, last, blank = _Row("6.0"), _Row("6.1.20"), _Row("  ")
        pr.sync(_Session([first, last, blank], players=800), apply=True, fetch=_fetch, min_players=50)
        assert (first.config_value, last.config_value) == ("6.0", "6.1.29")

    def test_no_row_yet_adds_one(self):
        session = _Session([], players=800)
        out = pr.sync(session, apply=True, fetch=_fetch, min_players=50)
        assert out["changed"] and len(session.added) == 1 and session.added[0].config_value == "6.1.29"
