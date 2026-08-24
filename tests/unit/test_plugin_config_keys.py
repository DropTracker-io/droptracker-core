"""Every group-config key the plugin's /load_config payload reads must be real.

``_group_configs_for`` (api/routes/players.py) hand-writes a registry key per
line, and ``get_config_value`` returns ``""`` for a key no group has ever
stored -- so a typo is silent: the endpoint keeps returning 200, and the plugin
just sees a field that is never set. That is how ``send_pets`` (the registry
key is ``notify_pets``) went unnoticed: Gson folds the empty string to
``false``, so every group was advertised as having pets switched off.

The keys are read straight out of the source with ``ast`` rather than by
importing the module: the endpoint pulls in the DB layer, and this assertion
only cares about the literals.
"""

import ast
import pathlib

import pytest

from web_api import config_registry as reg


_PLAYERS_PY = pathlib.Path(__file__).resolve().parents[2] / "api" / "routes" / "players.py"
_READER = "_group_configs_for"
_LOOKUP = "get_config_value"


def _config_keys_read_by(func_name: str, source_path: pathlib.Path):
    """Literal keys passed to get_config_value() inside the named function."""
    tree = ast.parse(source_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            break
    else:
        pytest.fail(f"{func_name}() not found in {source_path} -- did it get renamed?")

    keys = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        callee = call.func
        name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", None)
        if name != _LOOKUP:
            continue
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.append((arg.value, arg.lineno))
    return keys


class TestPluginLoadConfigKeys:
    def test_every_key_exists_in_the_registry(self):
        keys = _config_keys_read_by(_READER, _PLAYERS_PY)
        assert keys, f"no {_LOOKUP}() literals found in {_READER}()"

        unknown = [(k, line) for k, line in keys if reg.get_config_field(k) is None]
        assert not unknown, (
            "these keys are not in web_api/config_registry.py, so get_config_value "
            "silently returns '' for every group: "
            + ", ".join(f"{k!r} (players.py:{line})" for k, line in unknown)
        )

    def test_pet_notifications_read_the_registry_key(self):
        # The specific regression: the plugin field is send_pets, the config key
        # is notify_pets, and reading the field name as the key advertised every
        # group as pets-off.
        keys = {k for k, _ in _config_keys_read_by(_READER, _PLAYERS_PY)}
        assert "notify_pets" in keys
        assert "send_pets" not in keys
