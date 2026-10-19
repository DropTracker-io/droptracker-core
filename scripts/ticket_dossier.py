"""Read-only dossier of one support ticket, for investigating what it reports.

Prints the ticket row, its full transcript (with local paths for mirrored
attachments, so an agent can open the screenshots), the creator's linked
accounts and groups, and recent drops / notification rows for those accounts
plus any extra RSNs named in the ticket. Duplicate-shaped evidence is flagged
inline:

  * DUP-ROW   two ``drops`` rows for one player with the same item NAME and
              quantity within ``--dup-window`` seconds. Name, not id: a chat
              copy resolves the name to any of several ids (noted/unnoted,
              variants), and value drifts between GE lookups. A pair with a
              ``src=clan_chat`` row is a broadcast copy; two plugin rows are a
              plugin double-submit (multi-path loot sources).
  * DUP-POST  one drop with more than one ``notified`` row in the same group
              (send-side duplicate, e.g. a non-idempotent retry).
  * RETRIED   ``notification_queue`` rows whose error_message starts with
              "transient, retrying:" (the retry fingerprint).

The session is set READ ONLY and rolled back at exit, and every query carries
a ``max_statement_time`` cap. ``drops`` is only ever read by
``player_id`` + ``ORDER BY drop_id DESC LIMIT n``, which walks the player_id
index, so this is safe to run against prod.

Usage (always via the venv, from disc/):
    ./venv/bin/python -m scripts.ticket_dossier 491
    ./venv/bin/python -m scripts.ticket_dossier 491 --rsn "Some Name" --rsn Other
    ./venv/bin/python -m scripts.ticket_dossier 491 --drops 100 --since 2026-10-15
    ./venv/bin/python -m scripts.ticket_dossier 491 --transcript-only
    ./venv/bin/python -m scripts.ticket_dossier 491 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DISC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_ROOT = os.path.join(DISC_ROOT, "static", "assets", "img")
STATEMENT_SECONDS = 15


def _q(s, sql, **params):
    from sqlalchemy import text

    stmt = sql.lstrip()
    if stmt.upper().startswith("SELECT"):
        stmt = f"SET STATEMENT max_statement_time={STATEMENT_SECONDS} FOR " + stmt
    return [dict(r) for r in s.execute(text(stmt), params).mappings()]


def _attachments(raw):
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return [{"raw": raw}]
    out = []
    for e in entries if isinstance(entries, list) else []:
        e = dict(e)
        if e.get("path"):
            e["local_path"] = os.path.join(IMG_ROOT, e["path"])
            e["exists"] = os.path.exists(e["local_path"])
        out.append(e)
    return out


def _players_for(s, user_id, rsns):
    rows = []
    if user_id is not None:
        rows += _q(s, """
            SELECT player_id, player_name, wom_id, account_hash, user_id, account_type, date_added
            FROM players WHERE user_id = :u""", u=user_id)
    for name in rsns:
        rows += _q(s, """
            SELECT player_id, player_name, wom_id, account_hash, user_id, account_type, date_added
            FROM players WHERE player_name = :n""", n=name)
    seen, uniq = set(), []
    for r in rows:
        if r["player_id"] not in seen:
            seen.add(r["player_id"])
            uniq.append(r)
    return uniq


def _groups_for(s, player_ids):
    if not player_ids:
        return {}
    rows = _q(s, """
        SELECT uga.player_id, g.group_id, g.group_name
        FROM user_group_association uga JOIN `groups` g ON g.group_id = uga.group_id
        WHERE uga.player_id IN :pids""".replace(":pids", "(" + ",".join(str(int(p)) for p in player_ids) + ")"))
    out = defaultdict(list)
    for r in rows:
        out[r["player_id"]].append({"group_id": r["group_id"], "group_name": r["group_name"]})
    return dict(out)


def _drops_for(s, player_id, limit, since, dup_window):
    rows = _q(s, """
        SELECT d.drop_id, d.date_added, d.item_id, i.item_name, d.npc_id, n.npc_name,
               d.value, d.quantity, d.kill_count, d.unique_id, d.source, d.hidden,
               d.image_url IS NOT NULL AS has_image
        FROM drops d
        LEFT JOIN items i ON i.item_id = d.item_id
        LEFT JOIN npc_list n ON n.npc_id = d.npc_id
        WHERE d.player_id = :p
        ORDER BY d.drop_id DESC LIMIT :lim""", p=player_id, lim=limit)
    if since:
        rows = [r for r in rows if r["date_added"] and r["date_added"] >= since]
    # DUP-ROW: same content within the window. date_added is accept time
    # (not monotonic with drop_id), so compare on timestamps, not ids.
    by_key = defaultdict(list)
    for r in rows:
        name = (r.get("item_name") or "").lower() or r["item_id"]
        by_key[(name, r["quantity"])].append(r)
    for group in by_key.values():
        group.sort(key=lambda r: r["date_added"] or datetime.min)
        for a, b in zip(group, group[1:]):
            if a["date_added"] and b["date_added"] and \
                    (b["date_added"] - a["date_added"]).total_seconds() <= dup_window:
                for r, other in ((a, b), (b, a)):
                    r.setdefault("flags", []).append(f"DUP-ROW~{other['drop_id']}")
    return rows


def _notified_for(s, drop_ids):
    if not drop_ids:
        return {}
    rows = _q(s, """
        SELECT drop_id, group_id, channel_id, message_id, status, date_added
        FROM notified WHERE drop_id IN ({ids})""".format(ids=",".join(str(int(d)) for d in drop_ids)))
    out = defaultdict(list)
    for r in rows:
        out[r["drop_id"]].append(r)
    return dict(out)


def _queue_for(s, player_id, limit):
    rows = _q(s, """
        SELECT id, notification_type, group_id, status, created_at, processed_at, error_message,
               LEFT(data, 300) AS data_head
        FROM notification_queue WHERE player_id = :p
        ORDER BY id DESC LIMIT :lim""", p=player_id, lim=limit)
    for r in rows:
        if (r.get("error_message") or "").startswith("transient, retrying:"):
            r["flags"] = ["RETRIED"]
    return rows


def build(ticket_id, rsns, drops_limit, queue_limit, since, dup_window, transcript_only):
    from db.models.base import Session

    s = Session()
    try:
        s.execute(__import__("sqlalchemy").text("SET SESSION TRANSACTION READ ONLY"))
        ticket = _q(s, "SELECT * FROM tickets WHERE ticket_id = :t", t=ticket_id)
        if not ticket:
            return {"error": f"ticket {ticket_id} not found"}
        ticket = ticket[0]
        users = {}
        for key in ("created_by", "claimed_by", "closed_by"):
            uid = ticket.get(key)
            if uid is not None and uid not in users:
                u = _q(s, "SELECT user_id, username, discord_id, is_superadmin FROM users WHERE user_id = :u", u=uid)
                users[uid] = u[0] if u else None
        messages = _q(s, """
            SELECT id, discord_message_id, author_user_id, author_name, is_staff, is_bot, kind, origin,
                   content, attachments_json, date_sent, date_edited
            FROM ticket_messages WHERE ticket_id = :t ORDER BY date_sent, id""", t=ticket_id)
        for m in messages:
            m["attachments"] = _attachments(m.pop("attachments_json"))
        result = {"ticket": ticket, "users": users, "messages": messages}
        if transcript_only:
            return result

        players = _players_for(s, ticket.get("created_by"), rsns)
        groups = _groups_for(s, [p["player_id"] for p in players])
        accounts = []
        for p in players:
            drops = _drops_for(s, p["player_id"], drops_limit, since, dup_window)
            notified = _notified_for(s, [d["drop_id"] for d in drops])
            for d in drops:
                posts = notified.get(d["drop_id"], [])
                d["notified"] = posts
                per_group = defaultdict(int)
                for n in posts:
                    per_group[n["group_id"]] += 1
                for gid, count in per_group.items():
                    if count > 1:
                        d.setdefault("flags", []).append(f"DUP-POST g{gid}x{count}")
            accounts.append({
                "player": p,
                "groups": groups.get(p["player_id"], []),
                "drops": drops,
                "queue": _queue_for(s, p["player_id"], queue_limit),
            })
        missing = [n for n in rsns if not any(a["player"]["player_name"] and
                                              a["player"]["player_name"].lower() == n.lower() for a in accounts)]
        result["accounts"] = accounts
        result["unresolved_rsns"] = missing
        return result
    finally:
        s.rollback()
        s.close()


def _fmt(v):
    return v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v


def render(r):
    if "error" in r:
        print(r["error"])
        return
    t = r["ticket"]
    print(f"# Ticket {t['ticket_id']}  type={t['type']}  status={t['status']}  subject={t.get('subject')!r}")
    print(f"  opened {_fmt(t['date_added'])}  closed {_fmt(t.get('date_closed'))}  channel={t.get('channel_id')}")
    for key in ("created_by", "claimed_by", "closed_by"):
        uid = t.get(key)
        if uid is not None:
            u = r["users"].get(uid) or {}
            print(f"  {key}: user {uid} {u.get('username')!r} discord={u.get('discord_id')}")
    print("\n## Transcript")
    for m in r["messages"]:
        who = m["author_name"] + (" [staff]" if m["is_staff"] else "") + (" [bot]" if m["is_bot"] else "")
        print(f"\n--- {_fmt(m['date_sent'])} {who} ({m['origin']}/{m['kind']})")
        if m["content"]:
            print(m["content"])
        for a in m["attachments"]:
            loc = a.get("local_path") or a.get("url") or a.get("raw")
            print(f"  [attachment] {a.get('filename')} {a.get('content_type', '')} -> {loc}"
                  + ("" if a.get("exists", True) else " (MISSING ON DISK)"))
    if "accounts" not in r:
        return
    print("\n## Accounts")
    if r.get("unresolved_rsns"):
        print(f"  unresolved RSNs: {r['unresolved_rsns']}")
    for a in r["accounts"]:
        p = a["player"]
        gs = ", ".join(f"{g['group_id']}:{g['group_name']}" for g in a["groups"]) or "none"
        print(f"\n### player {p['player_id']} {p['player_name']!r} wom={p['wom_id']} user={p['user_id']} "
              f"type={p.get('account_type')} acc={p['account_hash']}")
        print(f"  groups: {gs}")
        print(f"  drops (newest first, {len(a['drops'])}):")
        for d in a["drops"]:
            posts = ",".join(f"g{n['group_id']}" for n in d["notified"]) or "-"
            flags = " ".join(d.get("flags", []))
            print(f"    {d['drop_id']} {_fmt(d['date_added'])} {d['item_name']!r} (id {d['item_id']}) x{d['quantity']} "
                  f"v={d['value']} npc={d['npc_name']!r} kc={d['kill_count']} src={d['source']} "
                  f"guid={d['unique_id']} img={int(bool(d['has_image']))} posted={posts} {flags}")
        print(f"  notification_queue (newest first, {len(a['queue'])}):")
        for q in a["queue"]:
            flags = " ".join(q.get("flags", []))
            err = (q.get("error_message") or "")[:120]
            print(f"    {q['id']} {_fmt(q['created_at'])} {q['notification_type']} g{q['group_id']} "
                  f"{q['status']} {err} {flags}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ticket_id", type=int)
    ap.add_argument("--rsn", action="append", default=[], help="extra RSN named in the ticket (repeatable)")
    ap.add_argument("--drops", type=int, default=50, help="recent drops per account (default 50, max 500)")
    ap.add_argument("--queue", type=int, default=30, help="recent notification_queue rows per account")
    ap.add_argument("--since", type=lambda v: datetime.fromisoformat(v), help="only drops on/after this date")
    ap.add_argument("--dup-window", type=int, default=10, help="seconds for DUP-ROW matching (default 10)")
    ap.add_argument("--transcript-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    r = build(args.ticket_id, args.rsn, min(args.drops, 500), min(args.queue, 200),
              args.since, args.dup_window, args.transcript_only)
    if args.json:
        print(json.dumps(r, default=_fmt, indent=1))
    else:
        render(r)


if __name__ == "__main__":
    main()
