"""Read-only dossier of one community suggestion or bug report.

Suggestions and bug reports share the ``suggestions`` table (``type`` is
``suggestion`` or ``bug``), mirrored 1:1 with posts in the two Discord forums.
The id is the ``#N`` in the Discord thread name (``<emoji> #N · <title>``) and
in droptracker.io/suggestions/N.

Prints the thread row, the full reply history, the author's site account,
linked accounts, groups and plugin versions, recent drops for those accounts
(bug reports only, unless ``--drops`` is given), and open/recent threads whose
titles share words with this one (likely duplicates or related reports).

The DB mirror keeps message text only. ``--discord`` also reads the live
Discord thread over REST (GET only) to list attachments/screenshots, which
are never mirrored, and downloads image attachments to /tmp so the Read tool
can open them.

The session is READ ONLY and rolled back at exit; every SELECT carries a
``max_statement_time`` cap; ``drops`` is read only through the player_id
index (shared helpers with scripts/ticket_dossier.py).

Usage (always via the venv, from disc/):
    ./venv/bin/python -m scripts.suggestion_dossier 512
    ./venv/bin/python -m scripts.suggestion_dossier 512 --kind bug --discord
    ./venv/bin/python -m scripts.suggestion_dossier 512 --rsn "Some Name" --drops 100
    ./venv/bin/python -m scripts.suggestion_dossier 512 --thread-only
    ./venv/bin/python -m scripts.suggestion_dossier 1234567890123456789   # Discord thread id
    ./venv/bin/python -m scripts.suggestion_dossier 512 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ticket_dossier import (  # noqa: E402
    _drops_for,
    _fmt,
    _groups_for,
    _notified_for,
    _players_for,
    _q,
    _queue_for,
)

DOWNLOAD_DIR = "/tmp/suggestion_dossier"
# Snowflakes are 17+ digits; suggestion ids will never get there.
SNOWFLAKE_MIN_DIGITS = 17
_STOPWORDS = {
    "the", "and", "for", "with", "not", "when", "from", "that", "this", "are",
    "was", "does", "doesnt", "dont", "can", "cant", "have", "add", "option",
    "please", "bug", "suggestion", "feature", "into", "being", "after", "its",
    "should", "would", "could", "all", "any", "but", "you", "your", "showing",
}


def _title_words(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return [w for w in dict.fromkeys(words) if len(w) >= 3 and w not in _STOPWORDS]


def _related(s, sug, limit=10):
    """Other threads sharing title words, ranked by overlap. The table is
    small (hundreds of rows), so a title scan is cheap."""
    words = _title_words(sug["title"])
    if not words:
        return []
    rows = _q(s, """
        SELECT id, type, title, is_open, message_count, created_at, last_activity_at
        FROM suggestions WHERE id <> :i ORDER BY id DESC LIMIT 3000""", i=sug["id"])
    scored = []
    for r in rows:
        hits = set(words) & set(_title_words(r["title"]))
        if len(hits) >= min(2, len(words)):
            r["shared"] = sorted(hits)
            scored.append(r)
    scored.sort(key=lambda r: (-len(r["shared"]), -r["id"]))
    return scored[:limit]


def _discord_get(path):
    import urllib.request

    token = os.getenv("BOT_TOKEN") or os.getenv("WEBHOOK_TOKEN")
    if not token:
        raise RuntimeError("no bot token in the environment")
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        headers={
            "Authorization": f"Bot {token}",
            # Cloudflare 403s urllib's default UA (error 1010).
            "User-Agent": "DiscordBot (https://www.droptracker.io, 1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _download(url, dest):
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "DiscordBot (https://www.droptracker.io, 1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())


def _discord_thread(thread_id, sug_id, download=True):
    """Live thread state + every message's attachments (GET only)."""
    out = {"thread": None, "messages": [], "error": None}
    try:
        ch = _discord_get(f"/channels/{thread_id}")
        meta = ch.get("thread_metadata") or {}
        out["thread"] = {
            "name": ch.get("name"),
            "parent_id": ch.get("parent_id"),
            "archived": meta.get("archived"),
            "locked": meta.get("locked"),
            "applied_tags": ch.get("applied_tags"),
            "message_count": ch.get("message_count"),
        }
        before, msgs = None, []
        for _ in range(10):  # up to 1000 messages
            q = "?limit=100" + (f"&before={before}" if before else "")
            page = _discord_get(f"/channels/{thread_id}/messages{q}")
            if not page:
                break
            msgs.extend(page)
            if len(page) < 100:
                break
            before = page[-1]["id"]
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    msgs.sort(key=lambda m: int(m["id"]))
    if download:
        os.makedirs(os.path.join(DOWNLOAD_DIR, str(sug_id)), exist_ok=True)
    for m in msgs:
        atts = []
        for a in m.get("attachments") or []:
            entry = {
                "filename": a.get("filename"),
                "content_type": a.get("content_type"),
                "size": a.get("size"),
                "url": a.get("url"),
            }
            if download and (a.get("content_type") or "").startswith("image/"):
                dest = os.path.join(DOWNLOAD_DIR, str(sug_id), f"{a['id']}_{a.get('filename')}")
                try:
                    _download(a["url"], dest)
                    entry["local_path"] = dest
                except Exception as e:  # noqa: BLE001
                    entry["download_error"] = str(e)
            atts.append(entry)
        embeds = [e.get("url") or e.get("title") for e in m.get("embeds") or [] if e.get("url") or e.get("title")]
        if not atts and not embeds:
            continue
        author = m.get("author") or {}
        out["messages"].append({
            "id": m["id"],
            "author": author.get("global_name") or author.get("username"),
            "bot": bool(author.get("bot")),
            "timestamp": m.get("timestamp"),
            "content_head": (m.get("content") or "")[:120],
            "attachments": atts,
            "embeds": embeds,
        })
    return out


def _plugin_versions(s, player_ids):
    if not player_ids:
        return {}
    rows = _q(s, """
        SELECT player_id, version, first_seen, last_seen, sightings, prerelease
        FROM player_plugin_versions WHERE player_id IN ({ids})
        ORDER BY last_seen DESC""".format(ids=",".join(str(int(p)) for p in player_ids)))
    out = {}
    for r in rows:
        out.setdefault(r["player_id"], []).append(r)
    return out


def build(ident, kind, rsns, drops_limit, queue_limit, since, dup_window, thread_only, discord, download):
    from sqlalchemy import text

    from db.models.base import Session

    s = Session()
    try:
        s.execute(text("SET SESSION TRANSACTION READ ONLY"))
        if len(str(ident)) >= SNOWFLAKE_MIN_DIGITS:
            rows = _q(s, "SELECT * FROM suggestions WHERE discord_thread_id = :t", t=str(ident))
        else:
            rows = _q(s, "SELECT * FROM suggestions WHERE id = :i", i=int(ident))
        if not rows:
            return {"error": f"no suggestion or bug report {ident}"}
        sug = rows[0]
        result = {"suggestion": sug, "warnings": []}
        if kind and sug["type"] != kind:
            result["warnings"].append(
                f"#{sug['id']} is a {sug['type']!r}, not a {kind!r} (ids are shared across both forums)")

        author = None
        if sug.get("user_id") is not None:
            u = _q(s, "SELECT user_id, username, discord_id, is_superadmin FROM users WHERE user_id = :u",
                   u=sug["user_id"])
            author = u[0] if u else None
        elif sug.get("author_discord_id"):
            # Discord-origin threads whose author linked an account later.
            u = _q(s, "SELECT user_id, username, discord_id, is_superadmin FROM users WHERE discord_id = :d",
                   d=sug["author_discord_id"])
            author = u[0] if u else None
        result["author"] = author

        result["messages"] = _q(s, """
            SELECT id, author_user_id, author_discord_id, author_name, source, content,
                   discord_message_id, created_at, edited_at
            FROM suggestion_messages WHERE suggestion_id = :i ORDER BY created_at, id""", i=sug["id"])
        result["related"] = _related(s, sug)

        if discord:
            if sug.get("discord_thread_id"):
                result["discord"] = _discord_thread(sug["discord_thread_id"], sug["id"], download)
            else:
                result["discord"] = {"error": f"no discord_thread_id (status={sug['status']})"}

        if thread_only:
            return result

        uid = author["user_id"] if author else None
        players = _players_for(s, uid, rsns)
        groups = _groups_for(s, [p["player_id"] for p in players])
        versions = _plugin_versions(s, [p["player_id"] for p in players])
        want_drops = drops_limit if drops_limit is not None else (50 if sug["type"] == "bug" else 0)
        accounts = []
        for p in players:
            acc = {
                "player": p,
                "groups": groups.get(p["player_id"], []),
                "plugin_versions": versions.get(p["player_id"], []),
            }
            if want_drops:
                drops = _drops_for(s, p["player_id"], want_drops, since, dup_window)
                notified = _notified_for(s, [d["drop_id"] for d in drops])
                for d in drops:
                    d["notified"] = notified.get(d["drop_id"], [])
                acc["drops"] = drops
                acc["queue"] = _queue_for(s, p["player_id"], queue_limit)
            accounts.append(acc)
        result["accounts"] = accounts
        result["unresolved_rsns"] = [
            n for n in rsns
            if not any((a["player"]["player_name"] or "").lower() == n.lower() for a in accounts)
        ]
        return result
    finally:
        s.rollback()
        s.close()


def render(r):
    if "error" in r:
        print(r["error"])
        return
    t = r["suggestion"]
    label = "Bug report" if t["type"] == "bug" else "Suggestion"
    print(f"# {label} #{t['id']}: {t['title']!r}")
    print(f"  origin={t['origin']} status={t['status']} open={bool(t['is_open'])} replies={t['message_count']}")
    print(f"  created {_fmt(t['created_at'])}  last activity {_fmt(t['last_activity_at'])}")
    print(f"  discord thread {t.get('discord_thread_id')}  https://www.droptracker.io/suggestions/{t['id']}")
    a = r.get("author")
    print(f"  author: {t.get('author_name')!r} discord={t.get('author_discord_id')} "
          + (f"-> user {a['user_id']} {a['username']!r} discord={a['discord_id']}" if a else "(no linked site account)"))
    for w in r.get("warnings", []):
        print(f"  WARNING: {w}")

    print("\n## Body")
    print(t["body_md"])
    print("\n## Replies")
    if not r["messages"]:
        print("  (none)")
    for m in r["messages"]:
        edited = f" (edited {_fmt(m['edited_at'])})" if m.get("edited_at") else ""
        uid = f" user {m['author_user_id']}" if m.get("author_user_id") is not None else ""
        print(f"\n--- {_fmt(m['created_at'])} {m['author_name']}{uid} via {m['source']}{edited}")
        print(m["content"])

    if "discord" in r:
        d = r["discord"]
        print("\n## Discord (live)")
        if d.get("error"):
            print(f"  could not read thread: {d['error']}")
        else:
            th = d["thread"] or {}
            print(f"  name={th.get('name')!r} archived={th.get('archived')} locked={th.get('locked')} "
                  f"tags={th.get('applied_tags')} messages={th.get('message_count')}")
            if not d["messages"]:
                print("  no attachments or embeds in the thread")
            for m in d["messages"]:
                print(f"  {m['timestamp'][:19]} {m['author']}{' [bot]' if m['bot'] else ''}: {m['content_head']!r}")
                for att in m["attachments"]:
                    loc = att.get("local_path") or att.get("url")
                    err = f" (download failed: {att['download_error']})" if att.get("download_error") else ""
                    print(f"    [attachment] {att['filename']} {att.get('content_type') or ''} -> {loc}{err}")
                for e in m["embeds"]:
                    print(f"    [embed] {e}")

    print("\n## Related threads (shared title words)")
    if not r["related"]:
        print("  (none)")
    for x in r["related"]:
        print(f"  #{x['id']} [{x['type']}{'' if x['is_open'] else ', closed'}] {x['title']!r} "
              f"replies={x['message_count']} created {_fmt(x['created_at'])[:10]} shared={x['shared']}")

    if "accounts" not in r:
        return
    print("\n## Author's accounts")
    if r.get("unresolved_rsns"):
        print(f"  unresolved RSNs: {r['unresolved_rsns']}")
    if not r["accounts"]:
        print("  (none linked)")
    for acc in r["accounts"]:
        p = acc["player"]
        gs = ", ".join(f"{g['group_id']}:{g['group_name']}" for g in acc["groups"]) or "none"
        print(f"\n### player {p['player_id']} {p['player_name']!r} wom={p['wom_id']} user={p['user_id']} "
              f"type={p.get('account_type')}")
        print(f"  groups: {gs}")
        vs = ", ".join(f"{v['version']} (last {_fmt(v['last_seen'])[:10]}{', pre' if v['prerelease'] else ''})"
                       for v in acc["plugin_versions"][:5]) or "none recorded"
        print(f"  plugin versions (newest first): {vs}")
        if "drops" not in acc:
            continue
        print(f"  drops (newest first, {len(acc['drops'])}):")
        for d in acc["drops"]:
            posts = ",".join(f"g{n['group_id']}" for n in d["notified"]) or "-"
            flags = " ".join(d.get("flags", []))
            print(f"    {d['drop_id']} {_fmt(d['date_added'])} {d['item_name']!r} (id {d['item_id']}) x{d['quantity']} "
                  f"v={d['value']} npc={d['npc_name']!r} kc={d['kill_count']} src={d['source']} "
                  f"img={int(bool(d['has_image']))} posted={posts} {flags}")
        print(f"  notification_queue (newest first, {len(acc['queue'])}):")
        for q in acc["queue"]:
            flags = " ".join(q.get("flags", []))
            err = (q.get("error_message") or "")[:120]
            print(f"    {q['id']} {_fmt(q['created_at'])} {q['notification_type']} g{q['group_id']} "
                  f"{q['status']} {err} {flags}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("id", help="suggestion/bug id (the #N), or the Discord thread id")
    ap.add_argument("--kind", choices=["suggestion", "bug"], help="warn if the id is the other type")
    ap.add_argument("--rsn", action="append", default=[], help="extra RSN named in the report (repeatable)")
    ap.add_argument("--drops", type=int, default=None,
                    help="recent drops per account (default 50 for bugs, 0 for suggestions; max 500)")
    ap.add_argument("--queue", type=int, default=30, help="recent notification_queue rows per account")
    ap.add_argument("--since", type=lambda v: datetime.fromisoformat(v), help="only drops on/after this date")
    ap.add_argument("--dup-window", type=int, default=10, help="seconds for DUP-ROW matching (default 10)")
    ap.add_argument("--thread-only", action="store_true", help="thread + replies only, no account lookups")
    ap.add_argument("--discord", action="store_true", help="also read the live Discord thread (attachments)")
    ap.add_argument("--no-download", action="store_true", help="with --discord, list attachments but don't save them")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    args.id = args.id.lstrip("#")
    if not args.id.isdigit():
        ap.error("id must be numeric")
    drops = None if args.drops is None else min(args.drops, 500)
    r = build(args.id, args.kind, args.rsn, drops, min(args.queue, 200), args.since,
              args.dup_window, args.thread_only, args.discord, not args.no_download)
    if args.json:
        print(json.dumps(r, default=_fmt, indent=1))
    else:
        render(r)


if __name__ == "__main__":
    main()
