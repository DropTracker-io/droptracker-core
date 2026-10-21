"""Anonymize the bot's own Discord posts about a removed player.

Deleting the posts would punch holes in clans' channel history, so a removal
edits them instead: every mention of the player's name becomes
``*Deleted user*``, and the screenshots go (uploaded attachments, embed
images, Components V2 media). Everything else in the post stays, so the
channel still reads in order.

``anonymize_payload`` is pure (a fetched message in, a PATCH body out, or
``None`` when nothing in it names the player) so it is tested without
Discord. ``anonymize_messages`` does the REST round-trips with the core bot's
token. Only messages the bot authored can be edited; webhook-sent posts come
back 403/50005 and are reported as skipped.
"""
from __future__ import annotations

import copy
import re
import time

DELETED_LABEL = "*Deleted user*"
IS_COMPONENTS_V2 = 1 << 15

# Component types (Discord API).
_ACTION_ROW, _SECTION, _TEXT, _THUMBNAIL, _GALLERY, _FILE, _CONTAINER = 1, 9, 10, 11, 12, 13, 17

_UPLOAD_MARKERS = ("/user-upload/", "attachment://", "cdn.discordapp.com/attachments/",
                   "media.discordapp.net/attachments/")


def name_pattern(names) -> re.Pattern | None:
    """One regex for every spelling of the player's name. OSRS treats space,
    '_' and '-' as the same character, so each matches all three; matching is
    case-insensitive and bounded so 'Fury' never hits 'Furyous'."""
    alts = []
    for n in sorted({n for n in names if n and n.strip()}, key=len, reverse=True):
        parts = [re.escape(p) for p in re.split(r"[ _\-]+", n.strip()) if p]
        if parts:
            alts.append(r"[ _\-]".join(parts))
    if not alts:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])", re.I)


def _scrub_text(s: str, pat: re.Pattern) -> str:
    if not s:
        return s
    # A markdown link whose label is the name loses its link too: it pointed
    # at the profile that no longer exists.
    s = re.sub(r"\[\s*\**(" + pat.pattern + r")\**\s*\]\([^)]*\)",
               DELETED_LABEL, s, flags=re.I)
    return pat.sub(DELETED_LABEL, s)


def _is_upload(url) -> bool:
    return isinstance(url, str) and any(m in url for m in _UPLOAD_MARKERS)


def _scrub_embed(e: dict, pat: re.Pattern) -> dict:
    e = copy.deepcopy(e)
    for key in ("title", "description"):
        if e.get(key):
            e[key] = _scrub_text(e[key], pat)
    for f in e.get("fields") or []:
        f["name"] = _scrub_text(f.get("name") or "", pat) or "​"
        f["value"] = _scrub_text(f.get("value") or "", pat) or "​"
    for key in ("author", "footer"):
        part = e.get(key)
        if isinstance(part, dict):
            for sub in ("name", "text"):
                if part.get(sub):
                    part[sub] = _scrub_text(part[sub], pat)
            if key == "author":
                part.pop("url", None)
    for key in ("image", "thumbnail"):
        if isinstance(e.get(key), dict) and _is_upload(e[key].get("url")):
            e.pop(key)
    # Read-only fields Discord returns but rejects on PATCH.
    for key in ("type", "video", "provider"):
        e.pop(key, None)
    for key in ("image", "thumbnail"):
        if isinstance(e.get(key), dict):
            e[key] = {"url": e[key].get("url")}
    if isinstance(e.get("author"), dict):
        e["author"].pop("proxy_icon_url", None)
    if isinstance(e.get("footer"), dict):
        e["footer"].pop("proxy_icon_url", None)
    return e


def _scrub_components(items: list, pat: re.Pattern) -> list:
    out = []
    for c in items or []:
        c = copy.deepcopy(c)
        t = c.get("type")
        if t == _TEXT:
            c["content"] = _scrub_text(c.get("content") or "", pat) or "​"
        elif t == _GALLERY:
            c["items"] = [i for i in c.get("items") or []
                          if not _is_upload((i.get("media") or {}).get("url"))]
            if not c["items"]:
                continue
        elif t == _FILE:
            continue
        elif t == _SECTION:
            c["components"] = _scrub_components(c.get("components") or [], pat)
            acc = c.get("accessory") or {}
            if acc.get("type") == _THUMBNAIL and _is_upload((acc.get("media") or {}).get("url")):
                # A section must carry an accessory; without the screenshot
                # its text stands on its own.
                out.extend(c["components"])
                continue
        elif t in (_CONTAINER, _ACTION_ROW):
            c["components"] = _scrub_components(c.get("components") or [], pat)
            if not c["components"]:
                continue
        if t == _ACTION_ROW:
            for b in c.get("components") or []:
                if b.get("label"):
                    b["label"] = _scrub_text(b["label"], pat)[:80]
        out.append(c)
    return out


_PATCH_STRIP = ("id", "proxy_url", "width", "height", "placeholder", "placeholder_version",
                "loading_state", "content_scan_metadata", "flags")


def _clean_media(node):
    """Drop response-only keys from unfurled media objects before PATCH."""
    if isinstance(node, dict):
        if "media" in node and isinstance(node["media"], dict):
            node["media"] = {"url": node["media"].get("url")}
        for v in node.values():
            _clean_media(v)
    elif isinstance(node, list):
        for v in node:
            _clean_media(v)


def anonymize_payload(message: dict, names) -> dict | None:
    """PATCH body that anonymizes ``message``, or None if there is nothing to
    change (the post doesn't name the player and carries no attachment)."""
    pat = name_pattern(names)
    if pat is None:
        return None
    flags = int(message.get("flags") or 0)
    has_files = bool(message.get("attachments"))
    if flags & IS_COMPONENTS_V2:
        comps = _scrub_components(message.get("components") or [], pat)
        _clean_media(comps)
        if comps == message.get("components") and not has_files:
            return None
        if not comps:
            comps = [{"type": _TEXT, "content": DELETED_LABEL}]
        return {"components": comps, "attachments": []}

    content = _scrub_text(message.get("content") or "", pat)
    embeds = [_scrub_embed(e, pat) for e in message.get("embeds") or []]
    comps = _scrub_components(message.get("components") or [], pat)
    _clean_media(comps)
    changed = (content != (message.get("content") or "")
               or any(_text_of(a) != _text_of(b) for a, b in zip(embeds, message.get("embeds") or []))
               or any(not _is_same_media(a, b) for a, b in zip(embeds, message.get("embeds") or []))
               or comps != (message.get("components") or []))
    if not changed and not has_files:
        return None
    body = {"content": content, "embeds": embeds, "attachments": []}
    if message.get("components"):
        body["components"] = comps
    return body


def _text_of(e: dict) -> str:
    import json
    return json.dumps({k: e.get(k) for k in ("title", "description", "fields", "author", "footer")},
                      sort_keys=True, default=str)


def _is_same_media(a: dict, b: dict) -> bool:
    return all(bool(a.get(k)) == bool(b.get(k)) for k in ("image", "thumbnail"))


def anonymize_messages(messages, names, token: str, *, apply: bool) -> dict:
    """Fetch, rewrite and PATCH each ``(channel_id, message_id)``."""
    import httpx

    out = {"edited": 0, "unchanged": 0, "gone": 0, "not_ours": 0, "failed": []}
    headers = {"Authorization": f"Bot {token}"}
    with httpx.Client(timeout=10.0, headers=headers) as client:
        def call(method, url, **kw):
            for _ in range(5):
                r = client.request(method, url, **kw)
                if r.status_code != 429:
                    return r
                time.sleep(float(r.json().get("retry_after", 1.0)) + 0.1)
            return r

        for channel_id, message_id in messages:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            r = call("GET", url)
            if r.status_code == 404:
                out["gone"] += 1
                continue
            if r.status_code != 200:
                out["failed"].append((channel_id, message_id, r.status_code))
                continue
            body = anonymize_payload(r.json(), names)
            if body is None:
                out["unchanged"] += 1
                continue
            if not apply:
                out["edited"] += 1
                continue
            p = call("PATCH", url, json=body)
            if p.status_code == 200:
                out["edited"] += 1
            elif p.status_code == 403 or (p.status_code == 400 and "50005" in p.text):
                out["not_ours"] += 1
            elif p.status_code == 404:
                out["gone"] += 1
            else:
                out["failed"].append((channel_id, message_id, p.status_code, p.text[:200]))
    return out
