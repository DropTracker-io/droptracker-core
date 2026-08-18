"""Unit tests for the pure pieces of the Discord group-onboarding panel:
input parsers, the permission checklist, registry-driven page building
blocks, and the invite-bitfield parity between the bot and web_api."""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# conftest stubs `interactions` flat; the modules under test import subpaths.
for _name in ("interactions.api", "interactions.api.events"):
    sys.modules.setdefault(_name, MagicMock())


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# guild_permissions builds its checklist from real interactions.Permissions
# flags — the conftest stub would make them MagicMocks, so pin real values.
class _Perm(int):
    pass


_FLAGS = {
    "VIEW_CHANNEL": 0x400, "SEND_MESSAGES": 0x800, "EMBED_LINKS": 0x4000,
    "ATTACH_FILES": 0x8000, "READ_MESSAGE_HISTORY": 0x10000,
    "MANAGE_CHANNELS": 0x10, "MANAGE_ROLES": 0x10000000,
    "ADMINISTRATOR": 0x8,
}
_perm_ns = MagicMock()
for _n, _v in _FLAGS.items():
    setattr(_perm_ns, _n, _Perm(_v))
sys.modules["interactions"].Permissions = _perm_ns

gp = _load("_guild_permissions_ut", "services/guild_permissions.py")


def test_invite_permissions_matches_web_api_literal():
    # web_api/routes/meta.py can't import the interactions lib, so it carries
    # the bitfield as a literal — this pins the two equal.
    import re

    src = open(os.path.join(_ROOT, "web_api", "routes", "meta.py")).read()
    m = re.search(r'or "(\d+)"', src)
    assert m, "meta.py invite default literal not found"
    assert int(m.group(1)) == int(gp.INVITE_PERMISSIONS)


def test_permission_checklist_admin_short_circuits():
    report = gp.permission_checklist(0x8)
    assert report["admin"] and report["ok"]
    assert all(ok for _l, ok in report["required"])


def test_permission_checklist_flags_missing():
    perms = 0x400 | 0x800  # view + send only
    report = gp.permission_checklist(perms)
    assert not report["ok"]
    missing = {label for label, ok in report["required"] if not ok}
    assert missing == {"Embed Links", "Attach Files", "Read Message History"}
    text = gp.render_checklist(report)
    assert "Re-check" in text


# The panel module needs db stubs only at import time (conftest provides
# them); its pure helpers touch neither Discord nor the DB. Its two service
# imports resolve via sys.modules ('services' is a stub, not a package).
sys.modules.setdefault("services.guild_permissions", gp)
sys.modules.setdefault("services.group_config_writer", MagicMock())
panel = _load("_group_onboarding_panel_ut", "services/group_onboarding_panel.py")


def test_parse_wom_input_forms():
    assert panel.parse_wom_input("1234") == 1234
    assert panel.parse_wom_input(" 1,234 ") == 1234
    assert panel.parse_wom_input("https://wiseoldman.net/groups/987") == 987
    assert panel.parse_wom_input("wiseoldman.net/groups/55?tab=x") == 55
    assert panel.parse_wom_input("clan chat") is None
    assert panel.parse_wom_input("") is None


def test_parse_gp_shorthand():
    assert panel.parse_gp("2500000") == 2_500_000
    assert panel.parse_gp("2.5m") == 2_500_000
    assert panel.parse_gp("500k") == 500_000
    assert panel.parse_gp("1b") == 1_000_000_000
    assert panel.parse_gp("2,500,000") == 2_500_000
    assert panel.parse_gp("-5") is None
    assert panel.parse_gp("abc") is None


def test_stored_bool_semantics():
    assert panel.stored_bool("", True) is True     # unset -> default
    assert panel.stored_bool("", False) is False
    assert panel.stored_bool("1", False) is True   # web-written
    assert panel.stored_bool("true", False) is True
    assert panel.stored_bool("0", True) is False


def test_hidden_keys_never_reach_the_panel():
    # Real registry (pure module). Every field the panel exposes must be
    # non-secret, non-bot-written, and carry the label/category metadata the
    # pages are built from.
    fields = panel._fields()
    keys = {f["key"] for f in fields}
    assert not (keys & panel.HIDDEN_KEYS)
    assert "group_name" not in keys
    assert all(f.get("label") and f.get("category") for f in fields)


def test_category_pages_cover_all_visible_fields():
    cats = {c["key"] for c in panel._categories()}
    for f in panel._fields():
        assert f["category"] in cats
    covered = set()
    for cat in cats:
        covered |= {f["key"] for f in panel.category_fields(cat)}
    assert covered == {f["key"] for f in panel._fields()}


def test_essential_toggles_are_real_boolean_fields():
    for key in panel.ESSENTIAL_TOGGLES:
        field = panel._field(key)
        assert field is not None, key
        assert field["type"] == "boolean", key
