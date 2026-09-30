"""Plugin settings snapshots (``type=config_snapshot``, plugin 6.0.16+).

The plugin sends its settings as JSON in the embed *description*: around 45
settings would blow Discord's 25-field embed limit, so the usual one field per
value does not fit. The snapshot is stored as the player's latest
(``db.models.PlayerPluginConfig``) for group leaders and staff to read while
debugging a player's setup. No Discord post, no chat notice.

Identity is the account hash alone, with no auth round trip and no player
creation: a snapshot for an account we have never seen a submission from has
nothing to attach to and is dropped.
"""
import json

from sqlalchemy.exc import SQLAlchemyError

from db.models import Player, PlayerPluginConfig

from .common import SubmissionResponse, received_at, select_session_and_flag

#: The embed description limit is 4096; anything past it did not come from
#: the plugin.
MAX_SNAPSHOT_CHARS = 4096

#: Where the intake parsers put the embed description
#: (api.routes.webhook.process_webhook_data, bots.webhook_bot._embed_to_dict).
DESCRIPTION_KEY = "_embed_description"


def parse_snapshot(raw):
    """The snapshot dict, or None when ``raw`` is not a plausible snapshot."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_SNAPSHOT_CHARS:
        return None
    text = raw.strip()
    # Tolerate a Discord code fence, should a client ever wrap it in one.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        snapshot = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("settings"), dict):
        return None
    return snapshot


async def config_snapshot_processor(data, external_session=None):
    session, _use_external_session = select_session_and_flag(external_session)

    snapshot = parse_snapshot(data.get(DESCRIPTION_KEY))
    if snapshot is None:
        return SubmissionResponse(False, "Missing or unreadable config snapshot")
    account_hash = data.get("acc_hash")
    if not account_hash:
        return SubmissionResponse(False, "Config snapshot has no account")

    player = (
        session.query(Player)
        .filter(Player.account_hash == str(account_hash))
        .first()
    )
    if player is None:
        return SubmissionResponse(True, "No player for this account yet; snapshot ignored")

    env = snapshot.get("env") if isinstance(snapshot.get("env"), dict) else {}
    config_json = json.dumps(snapshot, separators=(",", ":"))
    config_hash = str(snapshot.get("hash") or "")[:64] or None
    captured = received_at(data)
    used_api = data.get("used_api")

    try:
        row = session.get(PlayerPluginConfig, player.player_id)
    except SQLAlchemyError:
        # Table not migrated yet (web125a): the code can ship ahead of the
        # migration without turning every snapshot into a consumer error.
        session.rollback()
        return SubmissionResponse(False, "Config snapshot storage is not available yet")
    if row is None:
        row = PlayerPluginConfig(player_id=player.player_id)
        session.add(row)
    elif config_hash is None or row.config_hash != config_hash:
        # Keep the one before, so a viewer can show what just changed. A
        # resend of the same settings only refreshes the timestamp.
        row.previous_config_json = row.config_json
        row.previous_captured_at = row.captured_at
    row.config_json = config_json
    row.config_hash = config_hash
    row.plugin_version = str(data.get("p_v") or "")[:32] or None
    row.runelite_version = str(env.get("runelite_version") or "")[:32] or None
    row.used_api = bool(used_api) if used_api is not None else None
    row.captured_at = captured
    return SubmissionResponse(True, "Plugin configuration saved")
