"""Custom bot profile (web139a, services/bot_profile.py).

What matters most is what reaches Discord: a PATCH must never carry a key we
did not mean to change (interactions' modify_current_member sends nick: null
and wipes the nickname), a lapse must send the full revert, and a failed
entitlement lookup must never be read as a lapse.
"""
import asyncio
import io
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from services import bot_profile as bp


def _snap(**kw):
    base = dict(group_id=7, group_name="Iron Gang", guild_id="111", applied_guild_id="111",
                entitled=True,
                desired={"nick": None, "bio": None, "avatar_url": None, "banner_url": None},
                applied={"nick": None, "bio": None, "avatar_url": None, "banner_url": None})
    base.update(kw)
    return bp.Snapshot(**base)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
class TestNick:
    def test_blank_means_default(self):
        assert bp.clean_nick(None) is None
        assert bp.clean_nick("   ") is None

    def test_collapses_whitespace(self):
        assert bp.clean_nick("  Loot   Goblin ") == "Loot Goblin"

    def test_length_cap(self):
        assert bp.clean_nick("x" * 32) == "x" * 32
        with pytest.raises(bp.ProfileValidationError):
            bp.clean_nick("x" * 33)

    @pytest.mark.parametrize("name", ["Discord", "D1sc0rd Bot", "Drop Tracker", "droptracker.io",
                                      "Wise-Old-Man", "RuneLite", "JAGEX mod", "clyde"])
    def test_impersonation_blocked(self, name):
        with pytest.raises(bp.ProfileValidationError):
            bp.clean_nick(name)

    @pytest.mark.parametrize("name", ["Iron Admins", "Clan Staff", "Loot Bot", "Moderator Mike"])
    def test_ordinary_clan_names_allowed(self, name):
        assert bp.clean_nick(name) == name

    def test_rejects_non_text(self):
        with pytest.raises(bp.ProfileValidationError):
            bp.clean_nick(123)


class TestBio:
    def test_blank_means_none(self):
        assert bp.clean_bio("\n \n") is None

    def test_keeps_lines_but_squeezes_gaps(self):
        assert bp.clean_bio("Hi\n\n\n\nthere  friend") == "Hi\n\nthere friend"

    def test_caps(self):
        with pytest.raises(bp.ProfileValidationError):
            bp.clean_bio("x" * (bp.BIO_MAX + 1))
        with pytest.raises(bp.ProfileValidationError):
            bp.clean_bio("\n".join("a" for _ in range(7)))


class TestImages:
    def _png(self, size, fmt="PNG"):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", size, (200, 30, 30)).save(buf, format=fmt)
        return buf.getvalue()

    def test_avatar_cropped_square(self):
        from PIL import Image

        out = bp.process_image(self._png((900, 300)), "avatar")
        im = Image.open(io.BytesIO(out))
        assert im.format == "PNG" and im.size == bp.AVATAR_SIZE

    def test_banner_shape(self):
        from PIL import Image

        out = bp.process_image(self._png((300, 300), "JPEG"), "banner")
        assert Image.open(io.BytesIO(out)).size == bp.BANNER_SIZE

    def test_rejects_garbage_and_gif(self):
        with pytest.raises(bp.ProfileValidationError):
            bp.process_image(b"not an image", "avatar")
        with pytest.raises(bp.ProfileValidationError):
            bp.process_image(self._png((10, 10), "GIF"), "avatar")
        with pytest.raises(bp.ProfileValidationError):
            bp.process_image(b"", "avatar")


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
class TestPlanning:
    def test_entitled_targets_desired(self):
        snap = _snap(desired={"nick": "Goblin", "bio": None, "avatar_url": "u1", "banner_url": None})
        assert bp.target_state(snap) == {"nick": "Goblin", "bio": None, "avatar_url": "u1", "banner_url": None}

    def test_lapsed_targets_default(self):
        snap = _snap(entitled=False, desired={"nick": "Goblin", "bio": "b", "avatar_url": "u1", "banner_url": "u2"})
        assert bp.target_state(snap) == {f: None for f in bp.ALL_FIELDS}

    def test_only_changed_fields(self):
        target = {"nick": "Goblin", "bio": "b", "avatar_url": "u1", "banner_url": None}
        applied = {"nick": "Goblin", "bio": None, "avatar_url": "u0", "banner_url": None}
        assert bp.plan_changes(target, applied) == {"bio": "b", "avatar_url": "u1"}

    def test_empty_string_equals_none(self):
        assert bp.plan_changes({"nick": ""}, {"nick": None}) == {}

    def test_nick_goes_alone(self):
        batches = bp.split_requests({"nick": "G", "bio": "b", "avatar_url": "u"})
        assert batches == [{"nick": "G"}, {"bio": "b", "avatar_url": "u"}]

    def test_payload_never_carries_unchanged_keys(self):
        # The modify_current_member trap: an avatar change must not send nick.
        payload = bp.discord_payload({"avatar_url": "u1"}, {"u1": "data:image/png;base64,AA"})
        assert payload == {"avatar": "data:image/png;base64,AA"}
        assert "nick" not in payload

    def test_lapse_payload_clears_everything_applied(self):
        applied = {"nick": "G", "bio": "b", "avatar_url": "u1", "banner_url": "u2"}
        changes = bp.plan_changes({f: None for f in bp.ALL_FIELDS}, applied)
        payloads = [bp.discord_payload(b, {}) for b in bp.split_requests(changes)]
        assert payloads == [{"nick": None}, {"bio": None, "avatar": None, "banner": None}]

    def test_status(self):
        assert bp.status_for({"nick": "G"}) == "applied"
        assert bp.status_for({f: None for f in bp.ALL_FIELDS}) == "default"


class TestSignature:
    def test_custom_bio_always_signed(self):
        signed = bp.with_signature("Ember Clan's loot bot")
        assert signed.endswith("Powered by The DropTracker\nhttps://www.droptracker.io")
        assert signed.startswith("Ember Clan's loot bot\n\n")

    def test_full_length_bio_fits_discord(self):
        assert bp.clean_bio("x" * bp.BIO_MAX) == "x" * bp.BIO_MAX
        assert len(bp.with_signature("x" * bp.BIO_MAX)) <= bp.DISCORD_BIO_MAX

    def test_older_long_bio_is_trimmed_not_unsigned(self):
        signed = bp.with_signature("x" * 190)
        assert len(signed) <= bp.DISCORD_BIO_MAX and signed.endswith(bp.BIO_SIGNATURE)

    def test_no_custom_bio_means_bots_own(self):
        assert bp.with_signature(None) is None
        snap = _snap(desired={"nick": "G", "bio": None, "avatar_url": None, "banner_url": None})
        assert bp.target_state(snap)["bio"] is None

    def test_target_signs_and_lapse_clears(self):
        desired = {"nick": None, "bio": "Hi", "avatar_url": None, "banner_url": None}
        assert bp.target_state(_snap(desired=dict(desired)))["bio"] == bp.with_signature("Hi")
        assert bp.target_state(_snap(entitled=False, desired=dict(desired)))["bio"] is None


class TestErrors:
    def _exc(self, status, text=""):
        e = Exception(text)
        e.status = status
        e.text = text
        return e

    def test_nick_403_names_the_permission(self):
        msg, backoff = bp.classify_error(self._exc(403), {"nick": "G"})
        assert "Change Nickname" in msg and backoff == bp.ACCESS_BACKOFF

    def test_404_bot_missing(self):
        msg, _ = bp.classify_error(self._exc(404), {"avatar_url": "u"})
        assert "not in your Discord server" in msg

    def test_timeout_is_rate_limit(self):
        _, backoff = bp.classify_error(asyncio.TimeoutError(), {"bio": "b"})
        assert backoff == bp.RATE_LIMIT_BACKOFF

    def test_avatar_too_fast(self):
        msg, backoff = bp.classify_error(self._exc(400, "You are changing your avatar too fast"), {"avatar_url": "u"})
        assert backoff == bp.RATE_LIMIT_BACKOFF


# --------------------------------------------------------------------------- #
# reconcile_group with a fake Discord
# --------------------------------------------------------------------------- #
class FakeHttp:
    def __init__(self, live_nick=None, fail=None):
        self.patches = []
        self.live_nick = live_nick
        self.fail = fail or {}

    async def request(self, route, payload=None, reason=None, **_):
        key = "nick" if "nick" in payload else "other"
        if key in self.fail:
            raise self.fail[key]
        self.patches.append((route.params["guild_id"] if hasattr(route, "params") else None, payload))
        return {"ok": True}

    async def get_member(self, guild_id, user_id):
        return {"nick": self.live_nick}


@pytest.fixture
def harness(monkeypatch):
    state = {"records": [], "audits": [], "feeds": []}

    class _Route:
        def __init__(self, method, path, **params):
            self.params = params

    import sys
    from unittest.mock import MagicMock

    route_mod = MagicMock()
    route_mod.Route = _Route
    monkeypatch.setitem(sys.modules, "interactions.api.http.route", route_mod)

    def _record(group_id, guild_id, sent, target, error=None, backoff=None, reset_applied=False):
        state["records"].append(dict(guild_id=guild_id, sent=sent, error=error, reset=reset_applied))

    monkeypatch.setattr(bp, "record_result", _record)
    monkeypatch.setattr(bp, "record_audit", lambda gid, action, before, after, actor_user_id=None:
                        state["audits"].append(action))

    async def _feed(bot, snap, before, after, *, lapsed):
        state["feeds"].append((lapsed, after))

    monkeypatch.setattr(bp, "post_staff_feed", _feed)

    async def _uri(url):
        return f"data:{url}"

    monkeypatch.setattr(bp, "_fetch_data_uri", _uri)
    monkeypatch.setattr(bp, "_guild_allowed", lambda gid: True)

    def run(snap, http, **kw):
        monkeypatch.setattr(bp, "load_snapshot", lambda gid: snap)
        bot = SimpleNamespace(http=http, user=SimpleNamespace(id=999))
        return asyncio.run(bp.reconcile_group(bot, snap.group_id, **kw))

    state["run"] = run
    return state


class TestReconcileSignature:
    def test_bio_applied_before_signature_is_resent(self, harness):
        http = FakeHttp()
        snap = _snap(desired={"nick": None, "bio": "Hi", "avatar_url": None, "banner_url": None},
                     applied={"nick": None, "bio": "Hi", "avatar_url": None, "banner_url": None})
        harness["run"](snap, http, check_drift=True)
        assert [p for _, p in http.patches] == [{"bio": bp.with_signature("Hi")}]


class TestReconcile:
    def test_applies_new_settings(self, harness):
        http = FakeHttp()
        snap = _snap(desired={"nick": "Goblin", "bio": None, "avatar_url": "u1", "banner_url": None})
        harness["run"](snap, http)
        assert [p for _, p in http.patches] == [{"nick": "Goblin"}, {"avatar": "data:u1"}]
        assert harness["records"][-1]["sent"] == {"nick": "Goblin", "avatar_url": "u1"}

    def test_lapse_reverts_and_audits(self, harness):
        http = FakeHttp()
        snap = _snap(entitled=False,
                     desired={"nick": "Goblin", "bio": None, "avatar_url": "u1", "banner_url": None},
                     applied={"nick": "Goblin", "bio": None, "avatar_url": "u1", "banner_url": None})
        harness["run"](snap, http)
        assert [p for _, p in http.patches] == [{"nick": None}, {"avatar": None}]
        assert harness["audits"] == ["bot_profile.lapse_revert"]
        assert harness["feeds"][0][0] is True

    def test_failed_entitlement_lookup_does_nothing(self, harness):
        http = FakeHttp()
        snap = _snap(entitled=None, applied={"nick": "Goblin", "bio": None, "avatar_url": None, "banner_url": None})
        harness["run"](snap, http)
        assert http.patches == [] and harness["records"] == []

    def test_in_sync_does_nothing(self, harness):
        http = FakeHttp()
        same = {"nick": "Goblin", "bio": None, "avatar_url": None, "banner_url": None}
        harness["run"](_snap(desired=dict(same), applied=dict(same)), http)
        assert http.patches == []

    def test_drift_puts_nick_back(self, harness):
        http = FakeHttp(live_nick="Someone's Edit")
        same = {"nick": "Goblin", "bio": None, "avatar_url": None, "banner_url": None}
        harness["run"](_snap(desired=dict(same), applied=dict(same)), http, check_drift=True)
        assert [p for _, p in http.patches] == [{"nick": "Goblin"}]

    def test_hand_set_nick_left_alone_when_we_never_set_one(self, harness):
        http = FakeHttp(live_nick="Their Name")
        harness["run"](_snap(), http, check_drift=True)
        assert http.patches == []

    def test_nick_403_still_applies_avatar(self, harness):
        err = Exception("Missing Permissions")
        err.status = 403
        http = FakeHttp(fail={"nick": err})
        snap = _snap(desired={"nick": "Goblin", "bio": None, "avatar_url": "u1", "banner_url": None})
        harness["run"](snap, http)
        # The avatar still lands; only the nickname waits for the permission,
        # and it is not recorded as applied.
        assert [p for _, p in http.patches] == [{"avatar": "data:u1"}]
        rec = harness["records"][-1]
        assert rec["sent"] == {"avatar_url": "u1"} and "Change Nickname" in rec["error"]

    def test_rate_limit_stops_the_pass(self, harness):
        err = Exception("slow down")
        err.status = 429
        http = FakeHttp(fail={"other": err})
        snap = _snap(desired={"nick": None, "bio": "b", "avatar_url": "u1", "banner_url": None})
        harness["run"](snap, http)
        assert http.patches == [] and harness["records"][-1]["error"]

    def test_backoff_skips_sweep_but_not_a_save(self, harness):
        http = FakeHttp()
        snap = _snap(desired={"nick": "Goblin", "bio": None, "avatar_url": None, "banner_url": None},
                     retry_after=datetime.now() + timedelta(minutes=5))
        harness["run"](snap, http)
        assert http.patches == []
        harness["run"](snap, http, force=True)
        assert [p for _, p in http.patches] == [{"nick": "Goblin"}]

    def test_moved_server_reverts_old_one_first(self, harness):
        http = FakeHttp()
        applied = {"nick": "Goblin", "bio": None, "avatar_url": None, "banner_url": None}
        snap = _snap(guild_id="222", applied_guild_id="111", desired=dict(applied), applied=dict(applied))
        harness["run"](snap, http)
        assert http.patches == [(111, {"nick": None}), (222, {"nick": "Goblin"})]
        assert harness["records"][-1]["reset"] is True


class TestStaffFeed:
    def test_embed_shows_before_and_after(self):
        snap = _snap()
        embed = bp.staff_feed_embed(snap, {"nick": None, "avatar_url": "old"},
                                    {"nick": "Goblin", "avatar_url": "new"}, lapsed=False, actor="andy")
        names = [f["name"] for f in embed["fields"]]
        assert embed["title"] == "Bot profile updated"
        assert "Changed by" in names and "Avatar" in names
        assert embed["thumbnail"]["url"] == "new"

    def test_lapse_title(self):
        embed = bp.staff_feed_embed(_snap(), {"nick": "G"}, {"nick": None}, lapsed=True)
        assert "subscription ended" in embed["title"]


class TestDefaults:
    def test_builds_from_discord_profile(self):
        d = bp.build_defaults(
            {"id": "42", "username": "DropTracker.io", "global_name": None, "avatar": "a_abc", "banner": "def"},
            {"description": "  Track your drops.  "},
        )
        assert d["name"] == "DropTracker.io"
        assert d["avatar_url"] == "https://cdn.discordapp.com/avatars/42/a_abc.gif?size=256"
        assert d["banner_url"] == "https://cdn.discordapp.com/banners/42/def.png?size=1024"
        assert d["bio"] == "Track your drops."

    def test_missing_pieces(self):
        d = bp.build_defaults({"id": "42", "username": "Bot"}, None)
        assert d == {"name": "Bot", "avatar_url": None, "banner_url": None, "bio": None}

    def test_fallback_without_redis(self, monkeypatch):
        monkeypatch.setattr(bp, "_redis", lambda: None)
        assert bp.load_defaults() == bp.FALLBACK_DEFAULTS


class TestStagedUrls:
    def test_only_this_groups_own_uploads(self, monkeypatch):
        monkeypatch.setenv("B2_CDN_BASE_URL", "https://cdn.example")
        ok = "https://cdn.example/dt_uploads/bot_profiles/7-avatar-abc123.png"
        assert bp.staged_url_ok(ok, 7, "avatar")
        assert not bp.staged_url_ok(ok, 8, "avatar")      # another group's upload
        assert not bp.staged_url_ok(ok, 7, "banner")      # wrong slot
        assert not bp.staged_url_ok("https://evil.example/x.png", 7, "avatar")
        assert not bp.staged_url_ok("https://cdn.example/dt_uploads/bot_profiles/7-avatar-../../x.png", 7, "avatar")
        assert not bp.staged_url_ok(None, 7, "avatar")


class TestCooldownPeek:
    def test_peek_does_not_claim(self, monkeypatch):
        class R:
            def __init__(self):
                self.store = {}

            def ttl(self, k):
                return 120 if k in self.store else -2

            def set(self, k, v, nx=False, ex=None):
                if nx and k in self.store:
                    return False
                self.store[k] = v
                return True

        r = R()
        monkeypatch.setattr(bp, "_redis", lambda: r)
        assert bp.image_cooldown_left(7, "avatar") == 0
        assert bp.image_cooldown_left(7, "avatar") == 0
        assert bp.claim_image_slot(7, "avatar") == 0
        assert bp.image_cooldown_left(7, "avatar") == 120
