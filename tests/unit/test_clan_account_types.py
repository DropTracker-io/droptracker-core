"""utils/clan_ranks.py account-type badge sources: WOM cache + roster lookup."""

from utils import clan_ranks


class _FakeRedis:
    def __init__(self):
        self.hashes = {}

    def pipeline(self):
        return self

    def delete(self, key):
        self.hashes.pop(key, None)

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def expire(self, *_a):
        return True

    def execute(self):
        return []

    def hget(self, key, field):
        value = self.hashes.get(key, {}).get(field)
        return value.encode() if value is not None else None


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def execute(self, *_a, **_k):
        self.calls += 1
        return _Rows(self.rows)


def _env(monkeypatch, rows, wom_id=555):
    fake = _FakeRedis()
    monkeypatch.setattr(clan_ranks, "_redis", lambda: fake)
    monkeypatch.setattr(clan_ranks, "_group_wom_id", lambda _s, _g: wom_id)
    clan_ranks._roster_types_cache.clear()
    return fake, _Session(rows)


def test_wom_types_store_only_modes_with_a_badge(monkeypatch):
    fake, _ = _env(monkeypatch, [])
    stored = clan_ranks.store_group_account_types(
        555, {"Iron Al": "ironman", "Main Mo": "regular", "Hc_Hal": "hardcore", "X": "unknown"}
    )
    assert stored == 2
    assert fake.hashes["clantype:555"] == {"iron al": "ironman", "hc hal": "hardcore_ironman"}


def test_state_sync_wins_and_knows_group_modes(monkeypatch):
    # (player_name, player_state.account_type varbit, players.account_type)
    fake, session = _env(monkeypatch, [("Gim Guy", 4, None), ("De Ironed", 0, None)])
    clan_ranks.store_group_account_types(555, {"De Ironed": "ironman"})
    assert clan_ranks.account_type_for_group_member(session, 7, "Gim_Guy") == "group_ironman"
    # The varbit says normal now; a stale WOM "ironman" must not resurrect it.
    assert clan_ranks.account_type_for_group_member(session, 7, "De Ironed") is None


def test_wom_fallback_for_players_without_the_plugin(monkeypatch):
    fake, session = _env(monkeypatch, [])
    clan_ranks.store_group_account_types(555, {"No Plugin": "ultimate"})
    assert clan_ranks.account_type_for_group_member(session, 7, "No Plugin") == "ultimate_ironman"
    assert clan_ranks.account_type_for_group_member(session, 7, "Stranger") is None


def test_roster_is_memoized_per_group(monkeypatch):
    _fake, session = _env(monkeypatch, [("A", 1, None)])
    for _ in range(3):
        assert clan_ranks.account_type_for_group_member(session, 7, "A") == "ironman"
    assert session.calls == 1
