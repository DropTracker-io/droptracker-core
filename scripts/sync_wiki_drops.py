"""Sync ``xenforo.dt_npc_loot`` from the OSRS Wiki's Bucket API.

History: this table was originally filled by the XenForo plugin's
``SemanticDropUpdater`` — a lazy, one-shot scrape of the wiki's Semantic
MediaWiki ``action=ask`` endpoint that only ever ran for an NPC with zero
rows. The wiki has since REMOVED SMW entirely (``action=ask`` is now an
unrecognized action), so that pipeline died silently: every NPC keeps
whatever snapshot its single scrape captured, and new NPCs get nothing.
Found via Kree'arra (2026-08-10), whose early snapshot predates the wiki
publishing computed gem/rare-drop-table lines on boss pages — so the event
source picker couldn't offer it for Uncut diamond while the other three GWD
bosses, re-scraped later, listed theirs.

The wiki's replacement is the Bucket extension: ``action=bucket`` with a
Lua-ish query DSL. Drop lines live in the ``dropsline`` bucket, one row per
drop-table line, carrying the same ``Drop JSON`` payload SMW used to expose
(≈39k rows wiki-wide, ~8 requests at the 5000-row page cap).

What this script does, per wiki page that resolves to one of our NPC names:

* build the desired row set ``(item_id, quantity, noted, rarity, rolls)``
  from the page's drop lines — items resolved by exact name to the LOWEST
  ``items.item_id`` (readers resolve variants back out by name, see
  ``db/item_sources.py``);
* replicate it under EVERY ``npc_list`` id sharing the name — the NPC-page
  route reads ``WHERE npc_id = :nid`` per form id, and ``item_sources``
  collapses by name with a tracked-id preference, so all forms must carry
  the rows;
* rewrite (DELETE + INSERT, one transaction per NPC name) ONLY when the
  desired set differs from what's stored — reruns are free, and a full run
  against fresh data touches nothing. This also retires the legacy data's
  per-revision duplicate rows (125k rows for ~62k distinct pairs) one NPC
  at a time as pages change.

Differences from the wiki data the legacy scrape stored:

* Rarity strings now carry thousands separators and decimals
  ("1/2,500.92") — parsed here; the old PHP regex would have read that as
  1/2.
* Drop JSON no longer has a ``Noted`` key; noted is inferred from the
  ``Name Notes`` annotation instead.

NPCs whose wiki page vanished (or was renamed) keep their existing rows —
this script only ever rewrites pages it can see in the bucket, so a wiki
outage or rename can't wipe data. Unmatched pages / unresolved item names
are reported so map gaps stay visible.

Usage
-----
    python -m scripts.sync_wiki_drops                 # dry run, full report
    python -m scripts.sync_wiki_drops --apply
    python -m scripts.sync_wiki_drops --page "Kree'arra" --apply
"""

import argparse
import json
import re
import struct
import sys
import time

import requests

sys.path.insert(0, ".")

API_URL = "https://oldschool.runescape.wiki/api.php"
USER_AGENT = "@joelhalen - www.droptracker.io"
#: Bucket API page size (server accepts 5000; ~39k rows total).
PAGE_SIZE = 5000
#: Courtesy pause between paged requests.
REQUEST_GAP_SECONDS = 1.0

#: Wiki page name -> our npc_list name(s). The wiki stores container/reward
#: drops on the container's page; we track them under the activity's name.
#: Descended from the legacy SemanticDropUpdater map, with fixes: beginner
#: caskets were missing, and the Gauntlet mapped to "Corrupted Gauntlet" —
#: a name that has never existed in npc_list (ours are "The Gauntlet" /
#: "The Corrupted Gauntlet"), so that page never ingested at all.
ALT_NAMES = {
    "Rewards Chest (Fortis Colosseum)": "Fortis Colosseum",
    "Ancient chest": ["Chambers of Xeric", "Chambers of Xeric Challenge Mode"],
    "Chest (Tombs of Amascut)": ["Tombs of Amascut", "Tombs of Amascut: Expert Mode"],
    "Chest (Barrows)": "Barrows",
    "Reward pool": "Tempoross",
    "Reward casket (beginner)": "Clue Scroll (Beginner)",
    "Reward casket (easy)": "Clue Scroll (Easy)",
    "Reward casket (medium)": "Clue Scroll (Medium)",
    "Reward casket (hard)": "Clue Scroll (Hard)",
    "Reward casket (elite)": "Clue Scroll (Elite)",
    "Reward casket (master)": "Clue Scroll (Master)",
}

#: Pages whose drop lines split by mode via a ``Dropped from`` anchor
#: ("Monumental chest#Hard Mode"). Maps anchor -> npc_list name(s); the
#: ``None`` key routes unanchored (shared) lines. Anchors not listed are
#: routed like ``None``.
ANCHOR_ROUTES = {
    "Monumental chest": {
        "Normal Mode": ["Theatre of Blood"],
        "Hard Mode": ["Theatre of Blood: Hard Mode"],
        None: ["Theatre of Blood", "Theatre of Blood: Hard Mode"],
    },
    "Reward Chest (The Gauntlet)": {
        "Corrupted": ["The Corrupted Gauntlet"],
        "Regular": ["The Gauntlet"],
        # Partial-completion / failure shards can come from either mode.
        None: ["The Gauntlet", "The Corrupted Gauntlet"],
    },
}

_FRACTION_RE = re.compile(r"([\d.,]+)\s*/\s*([\d.,]+)")


def _db_float(x: float) -> float:
    """What ``dt_npc_loot.rarity`` will read back for a value we insert.

    The column is a MySQL FLOAT: the insert is narrowed to single precision,
    and the driver then returns MariaDB's 6-significant-digit rendering of
    that float32. Desired rows must carry that exact value or the
    change-gate sees every NPC as stale forever."""
    return float(f"{struct.unpack('f', struct.pack('f', x))[0]:.6g}")


def parse_rarity(raw) -> float:
    """Wiki rarity string -> probability float (0.0 = unknown/unparseable).

    Handles "Always", "30/128", "1/2,500.92" (thousands separators AND
    decimals — the legacy PHP regex only matched digit runs, so it would
    have read that as 1/2), and bare numerics.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return 0.0
    if "always" in s.lower():
        return 1.0
    m = _FRACTION_RE.search(s.replace(",", ""))
    if m:
        try:
            denom = float(m.group(2))
            return float(m.group(1)) / denom if denom else 0.0
        except ValueError:
            return 0.0
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return 0.0


def parse_quantity(drop: dict) -> str:
    """Quantity display string, normalized to the legacy "low-high" form."""
    q = drop.get("Drop Quantity")
    if q in (None, "", 0):
        low, high = drop.get("Quantity Low"), drop.get("Quantity High")
        if low and high and low != high:
            q = f"{low}-{high}"
        else:
            q = high or low or "1"
    # The wiki renders ranges with an en dash; stored data uses "-".
    return str(q).replace("–", "-").replace("—", "-")[:50]


def fetch_dropslines(session: requests.Session) -> list[dict]:
    """Every ``dropsline`` bucket row: ``{page_name, item_name, drop_json}``."""
    rows: list[dict] = []
    offset = 0
    while True:
        query = (
            "bucket('dropsline')"
            ".select('page_name','item_name','drop_json','rare_drop_table')"
            f".limit({PAGE_SIZE}).offset({offset}).run()"
        )
        resp = session.get(
            API_URL,
            params={"format": "json", "action": "bucket", "query": query},
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"bucket query failed: {body['error']}")
        page = body.get("bucket") or []
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE
        time.sleep(REQUEST_GAP_SECONDS)


def desired_rows_for_page(
    page_name: str, page_rows: list[dict], item_ids: dict
) -> tuple[dict, set]:
    """(``{db npc name: row tuples}``, unresolved item names) for one page.

    Row tuple = (item_id, quantity, noted, rarity, rolls). Sets, so a line
    repeated on the page (or an identical line on a boss's several forms'
    tables) collapses; genuinely distinct tiers for one item survive.
    """
    anchor_route = ANCHOR_ROUTES.get(page_name)
    if anchor_route is None:
        base = ALT_NAMES.get(page_name, page_name)
        base_names = base if isinstance(base, list) else [base]

    out: dict[str, set] = {}
    unresolved: set = set()
    for row in page_rows:
        try:
            drop = json.loads(row["drop_json"])
        except (KeyError, TypeError, ValueError):
            continue
        name = drop.get("Dropped item") or row.get("item_name") or ""
        # Multi-variant items link as "Bird nest (egg)#Blue egg" — the base
        # page name is the item.
        item_id = item_ids.get(name) or item_ids.get(name.partition("#")[0])
        if item_id is None:
            if name:
                unresolved.add(name)
            continue
        if anchor_route is not None:
            anchor = (drop.get("Dropped from") or "").partition("#")[2] or None
            db_names = anchor_route.get(anchor) or anchor_route[None]
        else:
            db_names = base_names
        quantity = parse_quantity(drop)
        # Drop JSON lost its "Noted" key in the Bucket migration; the flag
        # now only survives as a "(noted)" annotation in the quantity or
        # name-notes text.
        noted = 1 if (
            "noted" in quantity.lower()
            or "noted" in str(drop.get("Name Notes") or "").lower()
        ) else 0
        try:
            rolls = int(drop.get("Rolls") or 1)
        except (TypeError, ValueError):
            rolls = 1
        line = (
            item_id,
            quantity,
            noted,
            _db_float(parse_rarity(drop.get("Rarity"))),
            rolls,
        )
        for db_name in db_names:
            out.setdefault(db_name, set()).add(line)
    return out, unresolved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--page", help="sync a single wiki page name")
    args = ap.parse_args()

    from sqlalchemy import bindparam, text

    from db import Session

    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT

    print("fetching dropsline bucket…", flush=True)
    all_rows = fetch_dropslines(http)
    by_page: dict[str, list[dict]] = {}
    for r in all_rows:
        by_page.setdefault(r.get("page_name") or "", []).append(r)
    print(f"{len(all_rows)} drop lines across {len(by_page)} wiki pages")

    session = Session()
    try:
        npc_ids: dict[str, list[int]] = {}
        for npc_id, name in session.execute(text(
            "SELECT npc_id, npc_name FROM npc_list"
        )):
            npc_ids.setdefault(name, []).append(int(npc_id))
        # Lowest id per item name — one deterministic id per name; readers
        # fan back out to every variant id by name (db/item_sources.py).
        item_ids = {
            name: int(iid)
            for name, iid in session.execute(text(
                "SELECT item_name, MIN(item_id) FROM items GROUP BY item_name"
            ))
        }

        pages_matched = pages_unmatched = npcs_changed = npcs_unchanged = 0
        rows_written = 0
        unmatched_pages: list[tuple[int, str]] = []
        unresolved_items: set = set()

        for page_name, page_rows in sorted(by_page.items()):
            if args.page and page_name != args.page:
                continue
            desired_by_name, unresolved = desired_rows_for_page(
                page_name, page_rows, item_ids
            )
            unresolved_items |= unresolved
            known = {n: rows for n, rows in desired_by_name.items() if n in npc_ids}
            if not known:
                pages_unmatched += 1
                unmatched_pages.append((len(page_rows), page_name))
                continue
            pages_matched += 1

            for db_name, desired in known.items():
                ids = npc_ids[db_name]
                current = {
                    nid: set() for nid in ids
                }
                for nid, iid, qty, noted, rarity, rolls in session.execute(
                    text(
                        "SELECT npc_id, item_id, quantity, noted, rarity, rolls "
                        "FROM xenforo.dt_npc_loot WHERE npc_id IN :ids"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"ids": ids},
                ):
                    current[int(nid)].add(
                        (int(iid), str(qty), int(noted), float(rarity), int(rolls))
                    )

                stale = [nid for nid in ids if current[nid] != desired]
                if not stale:
                    npcs_unchanged += 1
                    continue
                npcs_changed += 1
                rows_written += len(desired) * len(stale)
                print(
                    f"  {'would rewrite' if not args.apply else 'rewriting'} "
                    f"{db_name!r} (page {page_name!r}): ids {stale}, "
                    f"{len(desired)} lines each"
                )
                if not args.apply:
                    continue
                session.execute(
                    text(
                        "DELETE FROM xenforo.dt_npc_loot WHERE npc_id IN :ids"
                    ).bindparams(bindparam("ids", expanding=True)),
                    {"ids": stale},
                )
                session.execute(
                    text(
                        "INSERT INTO xenforo.dt_npc_loot "
                        "(npc_id, item_id, quantity, noted, rarity, rolls) "
                        "VALUES (:npc_id, :item_id, :quantity, :noted, :rarity, :rolls)"
                    ),
                    [
                        {
                            "npc_id": nid,
                            "item_id": iid,
                            "quantity": qty,
                            "noted": noted,
                            "rarity": rarity,
                            "rolls": rolls,
                        }
                        for nid in stale
                        for (iid, qty, noted, rarity, rolls) in sorted(desired)
                    ],
                )
                session.commit()

        print(
            f"\npages: {pages_matched} matched to NPCs, {pages_unmatched} with no "
            f"npc_list match; NPCs: {npcs_changed} changed, {npcs_unchanged} already "
            f"current; rows {'written' if args.apply else 'to write'}: {rows_written}"
        )
        if unresolved_items:
            print(f"unresolved item names ({len(unresolved_items)}): "
                  + ", ".join(sorted(unresolved_items)[:20])
                  + ("…" if len(unresolved_items) > 20 else ""))
        top_unmatched = sorted(unmatched_pages, reverse=True)[:15]
        if top_unmatched and not args.page:
            print("largest unmatched pages (drop lines, page):")
            for n, p in top_unmatched:
                print(f"  {n:4d}  {p}")
        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
