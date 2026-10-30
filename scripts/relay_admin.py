"""Manage Discord delivery relay integrations from the shell (web153a).

The staff API (``/api/v1/admin/discord-relay``) is the primary surface; this
is the same operations for a box without the admin page, following the
maintenance-script idiom: dry-run by default, ``--apply`` to write.

    venv/bin/python -m scripts.relay_admin list
    venv/bin/python -m scripts.relay_admin create --group 299 --guild 1234 \\
        --application-id 5678 --label TrueGritty --key-out ~/relay.key --apply
    venv/bin/python -m scripts.relay_admin create ... --bind-key 41 --apply
    venv/bin/python -m scripts.relay_admin set 3 --status active --apply
    venv/bin/python -m scripts.relay_admin rotate 3 --key-out ~/relay.key --apply
    venv/bin/python -m scripts.relay_admin log 3

A minted key is written to ``--key-out`` (mode 600), never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime


def _redis():
    from utils.redis import RedisClient

    return RedisClient().client


def _bump():
    try:
        _redis().incr("relay:routes:version")
    except Exception as exc:
        print(f"warning: could not bump relay:routes:version ({exc}); "
              "the core bot picks the change up within 30s anyway")


def _write_key(path: str, token: str) -> None:
    path = os.path.expanduser(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    print(f"key written to {path} (mode 600)")


def _show(session, row) -> dict:
    from db.models import ApiKey, DiscordRelayGuild

    guilds = [g for (g,) in session.query(DiscordRelayGuild.guild_id).filter(
        DiscordRelayGuild.integration_id == row.id).all()]
    key = session.query(ApiKey).filter(ApiKey.id == row.api_key_id).first() \
        if row.api_key_id else None
    return {
        "id": row.id, "group_id": row.group_id, "label": row.label,
        "application_id": row.application_id, "status": row.status,
        "features": row.features, "hold_minutes": row.hold_minutes,
        "guild_ids": guilds,
        "api_key": (f"dtk_{key.id}_{key.token_prefix}... "
                    f"({'revoked' if key.revoked_at else 'live'})") if key else None,
    }


def _mint(session, row):
    from db import api_keys as keys

    key_row, token = keys.create_key(
        session, group_id=int(row.group_id), scope="group",
        label=f"Discord relay: {row.label or row.id}"[:64])
    session.flush()
    row.api_key_id = key_row.id
    return token


def cmd_list(session, _args):
    from db.models import DiscordRelayIntegration

    for row in session.query(DiscordRelayIntegration).order_by(DiscordRelayIntegration.id):
        print(json.dumps(_show(session, row), default=str))


def cmd_create(session, args):
    from db.models import ApiKey, DiscordRelayGuild, DiscordRelayIntegration

    if not args.bind_key and not args.key_out:
        sys.exit("create needs --key-out (to mint) or --bind-key <id>")
    row = DiscordRelayIntegration(
        group_id=args.group, label=args.label or "", application_id=args.application_id,
        features=args.feature or ["events"], status="paused",
        hold_minutes=args.hold_minutes)
    session.add(row)
    session.flush()
    for g in args.guild:
        session.add(DiscordRelayGuild(integration_id=row.id, guild_id=str(g)))
    token = None
    if args.bind_key:
        key = session.query(ApiKey).filter(ApiKey.id == args.bind_key).first()
        if key is None or key.revoked_at or key.scope != "group" or key.group_id != args.group:
            sys.exit("--bind-key must be a live group key of the same group")
        row.api_key_id = key.id
    else:
        token = _mint(session, row)
    session.flush()
    print(json.dumps(_show(session, row), default=str))
    if not args.apply:
        session.rollback()
        print("dry run: nothing written (add --apply)")
        return
    session.commit()
    if token:
        _write_key(args.key_out, token)
    _bump()
    print("created PAUSED — `set <id> --status active --apply` once the bot is polling")


def _get(session, integration_id):
    from db.models import DiscordRelayIntegration

    row = session.query(DiscordRelayIntegration).filter(
        DiscordRelayIntegration.id == integration_id).first()
    if row is None:
        sys.exit(f"no integration {integration_id}")
    return row


def cmd_set(session, args):
    row = _get(session, args.id)
    before = _show(session, row)
    for field in ("status", "application_id", "label", "hold_minutes"):
        value = getattr(args, field)
        if value is not None:
            setattr(row, field, value)
    if args.feature:
        row.features = args.feature
    if row.status == "active" and not row.application_id:
        sys.exit("set --application-id before activating")
    session.flush()
    print("before:", json.dumps(before, default=str))
    print("after: ", json.dumps(_show(session, row), default=str))
    if not args.apply:
        session.rollback()
        print("dry run: nothing written (add --apply)")
        return
    session.commit()
    _bump()


def cmd_rotate(session, args):
    row = _get(session, args.id)
    from db.models import ApiKey

    old = session.query(ApiKey).filter(ApiKey.id == row.api_key_id).first() \
        if row.api_key_id else None
    if old is not None and old.revoked_at is None:
        old.revoked_at = datetime.utcnow()
    token = _mint(session, row)
    print(json.dumps(_show(session, row), default=str))
    if not args.apply:
        session.rollback()
        print("dry run: nothing written (add --apply)")
        return
    session.commit()
    _write_key(args.key_out, token)
    _bump()


def cmd_log(_session, args):
    from services import discord_relay_core as core

    r = _redis()
    print(json.dumps(core.stats(r, args.id), default=str))
    for entry in core.recent_log(r, args.id, args.limit):
        print(json.dumps(entry))


def main() -> None:
    p = argparse.ArgumentParser(description="Discord delivery relay integrations")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("create")
    c.add_argument("--group", type=int, required=True)
    c.add_argument("--guild", action="append", required=True)
    c.add_argument("--application-id")
    c.add_argument("--label")
    c.add_argument("--feature", action="append", choices=("events", "notifications"))
    c.add_argument("--hold-minutes", type=int, default=360)
    c.add_argument("--key-out")
    c.add_argument("--bind-key", type=int)
    c.add_argument("--apply", action="store_true")
    s = sub.add_parser("set")
    s.add_argument("id", type=int)
    s.add_argument("--status", choices=("active", "paused"))
    s.add_argument("--application-id")
    s.add_argument("--label")
    s.add_argument("--hold-minutes", type=int)
    s.add_argument("--feature", action="append", choices=("events", "notifications"))
    s.add_argument("--apply", action="store_true")
    rt = sub.add_parser("rotate")
    rt.add_argument("id", type=int)
    rt.add_argument("--key-out", required=True)
    rt.add_argument("--apply", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("id", type=int)
    lg.add_argument("--limit", type=int, default=30)
    args = p.parse_args()

    from db.models import Session

    session = Session()
    try:
        {"list": cmd_list, "create": cmd_create, "set": cmd_set,
         "rotate": cmd_rotate, "log": cmd_log}[args.cmd](session, args)
    finally:
        session.close()


if __name__ == "__main__":
    main()
