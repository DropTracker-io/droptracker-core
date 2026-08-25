#!/usr/bin/env python3
"""
Rank the items and NPCs Discord notifications actually mention, and pick a set
======================================================================

An application emoji set is a *budget*, not a catalogue: 2000 per app, ~278 of
which the rank and UI sets already hold. So the question this answers is not
"which items exist" (29k) but "which items and NPCs does a clan actually read in
Discord often enough that a glyph earns its slot".

**Where the counts come from.** Only drops keep a per-notification row
(``notified``), and only for drops — the clog/CA rows it used to write stopped
in 2025-05, and PBs never had any. Every other surface has to be counted from
its own submission table and scaled: one submission fans out to one
notification *per group the player is in*, so this measures that fan-out over a
recent window where ``notification_queue`` still holds the sent rows, and
applies it to the full history. The result is an estimate of Discord messages,
which is the thing being ranked, rather than of submissions, which is not.

| surface | counted from        | mentions       |
|---------|---------------------|----------------|
| drop    | ``notified``+``drops`` (exact) | item + NPC |
| clog    | ``collection``      | item + NPC     |
| pb      | ``personal_best``   | NPC            |
| ca      | ``combat_achievement`` → ``scripts/ca_tasks.json`` | NPC |
| pet     | ``player_pets``     | item (the pet) |

**Why raw frequency alone picks the wrong set.** A group may set
``minimum_value_to_notify`` to anything it likes, so one clan farming frost
dragons put "Frost dragon bones" 8th on the all-time drop list — 1628
notifications from *2 groups and 5 players*. Frequency is therefore always
paired with breadth (distinct groups for drops, distinct players elsewhere),
which is what separates "the whole platform sees this" from "one clan's
threshold is set to 1gp". Two rules pull in the other direction: a pet and a
250M+ drop are the most-read messages a clan gets however rare they are, so both
are admitted on importance instead of on volume.

Writes the manifest ``utils/game_emojis.py`` reads and
``scripts/seed_game_emojis.py`` uploads. Read-only against the database.

Usage:
    python scripts/rank_game_emojis.py --report              # ranked listing, writes nothing
    python scripts/rank_game_emojis.py --report --top 60
    python scripts/rank_game_emojis.py --write               # rewrite the manifest
    python scripts/rank_game_emojis.py --write --budget 1200
    python scripts/rank_game_emojis.py --explain "Twisted bow"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
from PIL import Image  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from utils.game_emojis import (  # noqa: E402
    ITEM_ART_DIR,
    MANIFEST_PATH,
    NPC_ART_DIR,
    emoji_name,
    item_key,
    npc_key,
)
from utils.npc_names import npc_match_key  # noqa: E402

CA_TASKS_PATH = PROJECT_ROOT / "scripts" / "ca_tasks.json"

#: Where a missing item icon is pulled from — the same source
#: utils/item_images.py and the lootboard generator already use.
RUNELITE_ICON_URL = "https://static.runelite.net/cache/item/icon/{item_id}.png"
USER_AGENT = "DropTracker/1.0 (+https://www.droptracker.io; game emoji sync)"

#: Slots to fill. The core app holds 270 rank_* + 8 UI emojis against Discord's
#: 2000, so this is comfortably inside the ceiling; the seeder re-checks against
#: the live count rather than trusting this number.
DEFAULT_BUDGET = 1000

#: Ceiling on the NPC pool. Only ~490 NPC names ever appear, so this is a
#: safety rail rather than the binding constraint — NPC_FLOOR is.
DEFAULT_NPC_SHARE = 0.35

#: Estimated notifications an NPC needs before it is worth a slot. The tail
#: below this is Wilderness key variants and slayer trash ("Bronze key purple",
#: "Frost Crab") at 5-40 mentions in two years, which nobody would recognise as
#: a glyph anyway. Everything above it is a boss, a raid, a clue tier or a
#: named slayer target. Items have no equivalent floor: they compete for a
#: fixed number of slots instead.
NPC_FLOOR = 100

#: Share of the item budget held back for entries admitted on importance rather
#: than volume — see RESCUE_AVG_VALUE. 3rd age, the sigils and the damaged Torva
#: pieces are the messages a clan pins, and on frequency alone every one of them
#: loses to "Mithril plateskirt (g)". Bounded so the trophy case can never eat
#: the set it is a footnote to.
TROPHY_RESERVE_SHARE = 0.15

#: Distinct groups a drop item/NPC needs before its volume is believed. Set
#: from the observed cliff: at 5 the ranking keeps 91.6% of all drop
#: notification volume and sheds every single-clan artefact (see the module
#: docstring). Raising it starts cutting real content, lowering it lets a
#: misconfigured clan buy a slot.
MIN_DROP_GROUPS = 5

#: Distinct players a clog/PB/CA/pet entity needs. These tables have no group
#: column — one submission row is one player — so breadth is measured in
#: players and the bar is lower.
MIN_PLAYERS = 3

#: A drop this big is the most-read message a clan gets all month. 3rd age and
#: the rarest sigils clear it while appearing in only 2-4 groups, which is
#: exactly the case the breadth gate would otherwise throw away.
RESCUE_AVG_VALUE = 50_000_000

#: The window used to measure notifications-per-submission. notification_queue
#: is pruned, so this has to stay inside what it still holds (~8 days).
FANOUT_WINDOW_DAYS = 7

#: Source rows that name no real entity. `unknown` is npc_list 13943, the
#: placeholder a clog with an unresolved source is filed under — 153k rows, far
#: and away the biggest "NPC" in the database and not a thing anyone can draw.
JUNK_NAMES = {"", "unknown", "null", "none", "other"}

#: ``ca_tasks.json`` names monsters the way the wiki does; ``npc_list`` names
#: them the way the plugin reports a kill. Five spellings disagree, and between
#: them they carry 21k CA notifications that would otherwise be attributed to a
#: boss with no id, no art and no slot — while the real boss loses the credit.
#:
#: Deliberately local to this script rather than added to
#: ``utils.npc_names.NPC_ALIASES``: that table is the platform's NPC identity
#: rule and feeds drop attribution and PB matching, so widening it to satisfy an
#: emoji set would change how submissions are filed. Keyed by match key.
CA_MONSTER_ALIASES = {
    # The Moons of Peril file all their loot under the chest they open.
    "moons-of-peril": "Lunar Chest",
    "chambers-of-xeric-cm": "Chambers of Xeric Challenge Mode",
    # Neither of these has an npc_list row of its own; both have one for the
    # thing a kill actually drops from.
    "wintertodt": "Supply crate (Wintertodt)",
    "tzhaar-ket-rak-s-challenges": "JalTok-Jad",
}

#: ``items.item_id`` for every row flagged ``noted``. Filled by :func:`collect`
#: and read by :meth:`Entity.ids_by_use`; module state because art selection
#: happens in three places (eligibility, the manifest, ``--explain``) and
#: threading a 29k-entry set through all of them buys nothing.
NOTED_ITEM_IDS: set = set()


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

def connect():
    """A read-only connection to the `data` schema."""
    import pymysql

    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASS"),
        database="data",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def query(conn, sql, args=None) -> list:
    with conn.cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchall()


def measure_fanout(conn) -> dict:
    """notifications ÷ submissions per surface, over the recent window.

    A player in three clans turns one clog into three Discord messages. Without
    this the surfaces are compared on submissions, which understates whichever
    one is most popular among multi-clan players. Falls back to 1.0 for a
    surface with no recent traffic rather than inventing a multiplier.
    """
    sent = {
        row["notification_type"]: row["n"]
        for row in query(conn, """
            SELECT notification_type, COUNT(*) n FROM notification_queue
            WHERE status = 'sent' AND created_at >= NOW() - INTERVAL %s DAY
            GROUP BY 1
        """, (FANOUT_WINDOW_DAYS,))
    }
    submitted = {
        "clog": query(conn, "SELECT COUNT(*) n FROM collection WHERE date_added >= NOW() - INTERVAL %s DAY",
                      (FANOUT_WINDOW_DAYS,))[0]["n"],
        "ca": query(conn, "SELECT COUNT(*) n FROM combat_achievement WHERE date_added >= NOW() - INTERVAL %s DAY",
                    (FANOUT_WINDOW_DAYS,))[0]["n"],
        "pb": query(conn, "SELECT COUNT(*) n FROM personal_best WHERE date_added >= NOW() - INTERVAL %s DAY",
                    (FANOUT_WINDOW_DAYS,))[0]["n"],
        "pet": query(conn, "SELECT COUNT(*) n FROM player_pets WHERE date_added >= NOW() - INTERVAL %s DAY",
                     (FANOUT_WINDOW_DAYS,))[0]["n"],
    }
    fanout = {"drop": 1.0}  # `notified` already counts messages, not submissions
    for surface, rows in submitted.items():
        fanout[surface] = round(sent.get(surface, 0) / rows, 3) if rows else 1.0
    return fanout


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

class Entity:
    """One item or NPC, with the evidence for its rank.

    ``ids`` is every source id that resolved to this name — noted and
    placeholder item ids share a name and must share one emoji, and NPC rows
    duplicated by casing ("Greater demon" / "Greater Demon") are one boss.
    """

    __slots__ = ("kind", "key", "name", "ids", "id_uses", "mentions", "groups",
                 "players", "avg_value", "max_value", "surfaces", "rescued", "reserved")

    def __init__(self, kind, key, name):
        self.kind, self.key, self.name = kind, key, name
        self.ids, self.mentions, self.groups, self.players = set(), 0.0, 0, 0
        self.avg_value, self.max_value, self.rescued = 0, 0, False
        #: Took a slot the frequency pass would not have given it.
        self.reserved = False
        #: id -> how many source rows used it. Which id is *canonical* is not
        #: recorded anywhere, and guessing "lowest" is wrong: "Coins" is both
        #: 617 and 995, and 995 is the one 1885 of 1892 drops actually carried.
        self.id_uses = defaultdict(int)
        self.surfaces = defaultdict(float)

    def note_id(self, entity_id, uses: int = 1) -> None:
        if entity_id is not None:
            self.ids.add(int(entity_id))
            self.id_uses[int(entity_id)] += int(uses)

    @property
    def breadth(self) -> int:
        """The gate's denominator: groups for drops, players everywhere else."""
        return max(self.groups, self.players)

    def ids_by_use(self) -> list:
        """Every id this name claims, best art first.

        A noted id loses to every un-noted one however popular it is: a noted
        item's icon is the *note* — a scrap of paper with a watermark, identical
        to every other noted item at emoji size. "Runite ore" resolves to 452 on
        popularity (958 of its drops carried the noted id) and 452 draws a note.
        """
        return sorted(
            self.ids,
            key=lambda i: (i in NOTED_ITEM_IDS, -self.id_uses.get(i, 0), i),
        )


def _clean(name) -> str:
    name = (name or "").strip()
    return "" if name.lower() in JUNK_NAMES else name


def collect(conn, fanout: dict) -> tuple[dict, dict]:
    """Every item and NPC a notification can name, with its evidence."""
    items: dict[str, Entity] = {}
    npcs: dict[str, Entity] = {}

    def item(name):
        key = item_key(name)
        return items.setdefault(key, Entity("item", key, name))

    def npc(name):
        key = npc_key(name)
        entity = npcs.setdefault(key, Entity("npc", key, name))
        # Casing variants are one boss; keep the best-capitalised spelling so
        # the manifest reads like the game does.
        if name and name[:1].isupper() and not entity.name[:1].isupper():
            entity.name = name
        return entity

    def absorb(rows, resolve, surface, scale, with_value=False):
        """Fold one name-level aggregate into its entity.

        Breadth has to be counted *per name*, not per (name, source) pair:
        "Adamantite bar" is 4 groups as item 2361 and 14 as its noted twin
        2362, and taking either — or the larger — reads as 4-14 groups when the
        true figure is 15. So every surface runs one query grouped by the name
        alone for the counts, and a second grouped by (name, id) purely to
        learn which ids that name covers.
        """
        for row in rows:
            name = _clean(row["name"])
            if not name:
                continue
            entity = resolve(name.strip())
            mentions = float(row["n"]) * scale
            entity.mentions += mentions
            entity.surfaces[surface] += mentions
            entity.groups = max(entity.groups, int(row.get("n_groups") or 0))
            entity.players = max(entity.players, int(row.get("n_players") or 0))
            if with_value:
                entity.avg_value = max(entity.avg_value, int(row["avg_value"] or 0))
                entity.max_value = max(entity.max_value, int(row["max_value"] or 0))

    def absorb_ids(rows, resolve):
        for row in rows:
            name = _clean(row["name"])
            if name and row["entity_id"] is not None:
                resolve(name.strip()).note_id(row["entity_id"], row["n"])

    # -- drops: the one surface with an exact per-notification record --------
    DROP_FROM = """
        FROM notified nt
        JOIN drops d ON d.drop_id = nt.drop_id
        LEFT JOIN items i ON i.item_id = d.item_id
        LEFT JOIN npc_list nl ON nl.npc_id = d.npc_id
        WHERE nt.drop_id IS NOT NULL
    """
    absorb(query(conn, f"""
        SELECT i.item_name AS name, COUNT(*) n,
               COUNT(DISTINCT nt.group_id) n_groups,
               COUNT(DISTINCT nt.player_id) n_players,
               ROUND(AVG(d.value)) avg_value, MAX(d.value) max_value
        {DROP_FROM} GROUP BY 1
    """), item, "drop", 1.0, with_value=True)
    absorb(query(conn, f"""
        SELECT nl.npc_name AS name, COUNT(*) n,
               COUNT(DISTINCT nt.group_id) n_groups,
               COUNT(DISTINCT nt.player_id) n_players
        {DROP_FROM} GROUP BY 1
    """), npc, "drop", 1.0)
    absorb_ids(query(conn, f"SELECT i.item_name AS name, d.item_id AS entity_id, "
                           f"COUNT(*) n {DROP_FROM} GROUP BY 1, 2"), item)
    absorb_ids(query(conn, f"SELECT nl.npc_name AS name, d.npc_id AS entity_id, "
                           f"COUNT(*) n {DROP_FROM} GROUP BY 1, 2"), npc)

    # -- collection log ------------------------------------------------------
    scale = fanout.get("clog", 1.0)
    CLOG_FROM = """
        FROM collection c
        LEFT JOIN items i ON i.item_id = c.item_id
        LEFT JOIN npc_list nl ON nl.npc_id = c.npc_id
    """
    absorb(query(conn, f"""
        SELECT i.item_name AS name, COUNT(*) n, COUNT(DISTINCT c.player_id) n_players
        {CLOG_FROM} GROUP BY 1
    """), item, "clog", scale)
    absorb(query(conn, f"""
        SELECT nl.npc_name AS name, COUNT(*) n, COUNT(DISTINCT c.player_id) n_players
        {CLOG_FROM} GROUP BY 1
    """), npc, "clog", scale)
    absorb_ids(query(conn, f"SELECT i.item_name AS name, c.item_id AS entity_id, "
                           f"COUNT(*) n {CLOG_FROM} GROUP BY 1, 2"), item)
    absorb_ids(query(conn, f"SELECT nl.npc_name AS name, c.npc_id AS entity_id, "
                           f"COUNT(*) n {CLOG_FROM} GROUP BY 1, 2"), npc)

    # -- personal bests ------------------------------------------------------
    scale = fanout.get("pb", 1.0)
    PB_FROM = "FROM personal_best p LEFT JOIN npc_list nl ON nl.npc_id = p.npc_id"
    absorb(query(conn, f"""
        SELECT nl.npc_name AS name, COUNT(*) n, COUNT(DISTINCT p.player_id) n_players
        {PB_FROM} GROUP BY 1
    """), npc, "pb", scale)
    absorb_ids(query(conn, f"SELECT nl.npc_name AS name, p.npc_id AS entity_id, "
                           f"COUNT(*) n {PB_FROM} GROUP BY 1, 2"), npc)

    # -- combat achievements: a task names a boss, not an item ---------------
    task_monster = {}
    if CA_TASKS_PATH.exists():
        for task in json.loads(CA_TASKS_PATH.read_text()).get("tasks", []):
            if task.get("name") and task.get("monster"):
                task_monster[task["name"].strip().lower()] = task["monster"].strip()
    # Keyed with npc_key, not npc_match_key: the two differ only in their
    # separator ("chambers_of_xeric" vs "chambers-of-xeric") and Entity.key uses
    # the former, so keying this the other way silently matched nothing and
    # every boss known ONLY from a CA task lost its id, its art and its slot.
    npc_ids = {npc_key(r["npc_name"]): int(r["npc_id"])
               for r in query(conn, "SELECT npc_name, npc_id FROM npc_list ORDER BY npc_id DESC")}
    scale = fanout.get("ca", 1.0)
    unmatched = 0
    for row in query(conn, """
        SELECT task_name, COUNT(*) n, COUNT(DISTINCT player_id) n_players
        FROM combat_achievement GROUP BY 1
    """):
        monster = task_monster.get((row["task_name"] or "").strip().lower())
        if not monster:
            unmatched += int(row["n"])
            continue
        monster = CA_MONSTER_ALIASES.get(npc_match_key(monster), monster)
        e = npc(monster)
        # A CA task's monster is a wiki name; it only earns an id if npc_list
        # already knows the boss under some spelling. Without one there is no
        # art and the selector will drop it, which is the correct outcome.
        if e.key in npc_ids:
            e.note_id(npc_ids[e.key], int(row["n"]))
        e.mentions += float(row["n"]) * scale
        e.surfaces["ca"] += float(row["n"]) * scale
        e.players = max(e.players, int(row["n_players"]))

    # -- pets ----------------------------------------------------------------
    scale = fanout.get("pet", 1.0)
    for row in query(conn, """
        SELECT pet_name AS name, item_id AS entity_id,
               COUNT(*) n, COUNT(DISTINCT player_id) n_players
        FROM player_pets WHERE item_id IS NOT NULL GROUP BY 1, 2
    """):
        if not _clean(row["name"]):
            continue
        e = item(row["name"].strip())
        e.note_id(row["entity_id"], row["n"])
        e.mentions += float(row["n"]) * scale
        e.surfaces["pet"] += float(row["n"]) * scale
        e.players = max(e.players, int(row["n_players"]))
        # A pet is the loudest message a clan gets. Skotos appears 194 times in
        # two years; on volume alone it would lose to a rune drop nobody reads.
        e.rescued = True

    # -- un-noted siblings ---------------------------------------------------
    # An item can reach here knowing only its noted id, because that is the one
    # the drop carried ("Magic logs" 1514, never 1513). The catalogue holds the
    # un-noted twin under the same name, so adopt it with a use count of zero:
    # ids_by_use will draw it, and every id stays in the entry so a noted drop
    # still resolves to the glyph.
    unnoted_by_key = defaultdict(list)
    for row in query(conn, "SELECT item_id, item_name, noted FROM items"):
        if row["noted"]:
            NOTED_ITEM_IDS.add(int(row["item_id"]))
        elif _clean(row["item_name"]):
            unnoted_by_key[item_key(row["item_name"])].append(int(row["item_id"]))
    for key, entity in items.items():
        if entity.ids and entity.ids <= NOTED_ITEM_IDS:
            for sibling in sorted(unnoted_by_key.get(key, ())):
                entity.note_id(sibling, 0)

    if unmatched:
        print(f"  note: {unmatched} CA notifications name a task with no monster in "
              f"{CA_TASKS_PATH.name} (mechanical/skilling tasks) — not attributed")
    return items, npcs


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def art_path(entity: Entity) -> Path | None:
    """The icon this entity would upload, or None if nothing is on disk.

    An entry with no art cannot be seeded, so it must not consume a slot. A name
    covers several ids — noted, placeholder and Leagues variants all share one —
    and the right one to draw is the one the data actually used, not the lowest:
    "Coins" is 617 and 995, 995 carried 1885 of the 1892 notifications, and 617
    is a different sprite.
    """
    directory = ITEM_ART_DIR if entity.kind == "item" else NPC_ART_DIR
    for entity_id in entity.ids_by_use():
        candidate = directory / f"{entity_id}.png"
        if candidate.exists():
            return candidate
    return None


def discount_unbelievable_drops(entity: Entity) -> None:
    """Strike drop volume that too few groups witnessed.

    Notification volume is only evidence of popularity if it came from more
    than one clan's settings. ``minimum_value_to_notify`` is per-group and
    unbounded downward, so one clan at 1gp farming frost dragons produced 1628
    notifications of "Frost dragon bones" — the 8th most-announced item on the
    platform, from 2 groups and 5 players.

    Removing the volume rather than the entity is what keeps this honest: an
    item that also unlocks a collection log slot keeps that evidence and is
    ranked on it, while an item whose *only* claim was one clan's threshold
    drops to zero and falls out on its own.
    """
    if entity.surfaces.get("drop") and entity.groups < MIN_DROP_GROUPS:
        entity.mentions -= entity.surfaces.pop("drop")
        entity.groups = 0


def fetch_missing_item_art(entities) -> int:
    """Pull item icons RuneLite has but this box does not, once, before ranking.

    ``static/assets/img/itemdb/`` was backfilled for the whole catalogue in
    2026-07, so a gap means an item minted since — which is precisely the
    content a clan is announcing right now. "Aggy" and "Mr mcgroot", two pets
    from the current update, were being cut for "no icon on disk" while their
    icons sat on RuneLite's CDN. Best-effort: a failure leaves the entity
    ineligible, which is the same outcome as not trying.

    NPC renders have no equivalent source — they come from the wiki via
    ``scripts/backfill_npc_images.py`` — so only items are fetched here.
    """
    fetched = 0
    for entity in entities:
        if entity.kind != "item" or art_path(entity) is not None:
            continue
        for entity_id in entity.ids_by_use():
            destination = ITEM_ART_DIR / f"{entity_id}.png"
            try:
                request = urllib.request.Request(
                    RUNELITE_ICON_URL.format(item_id=entity_id),
                    headers={"User-Agent": USER_AGENT},
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = response.read()
            except (urllib.error.URLError, OSError):
                continue
            if not data:
                continue
            # Atomically, so a concurrent image-server read never sees a
            # partial file (same contract as utils/item_images.py).
            temporary = destination.with_suffix(f".png.tmp.{os.getpid()}")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
            fetched += 1
            break
    return fetched


#: Fraction of an image's height that may be full-width opaque before the art is
#: judged a screenshot. A model render is a silhouette — no row of it spans the
#: canvas — so the measure separates cleanly: the six offenders all score above
#: 0.63 and the next-worst real render (the Chambers of Xeric banner, which is a
#: logo and reads fine small) scores 0.196.
SCREENSHOT_ROW_RUN = 0.5

_art_quality_cache: dict = {}


def screenshot_like(path: Path) -> bool:
    """Whether this art is a screen capture rather than a cut-out render.

    Activities with no single model — Barrows, Fortis Colosseum, the Gauntlet —
    are illustrated on the wiki with a photograph of the room, letterboxed onto
    the canvas. Scaled to the 22 px Discord renders an emoji at, that is an
    unreadable smear of brown, and a reader is better served by the plain name
    the caller falls back to. The alpha channel is sampled at 64x64: a run of
    fully-opaque full-width rows survives the downscale, a silhouette's ragged
    edge does not become one.
    """
    if path in _art_quality_cache:
        return _art_quality_cache[path]
    try:
        alpha = Image.open(path).convert("RGBA").getchannel("A").resize((64, 64), Image.NEAREST)
    except (OSError, ValueError):
        _art_quality_cache[path] = False
        return False
    pixels, longest, run = alpha.load(), 0, 0
    for y in range(64):
        run = run + 1 if all(pixels[x, y] > 200 for x in range(64)) else 0
        longest = max(longest, run)
    verdict = longest / 64 > SCREENSHOT_ROW_RUN
    _art_quality_cache[path] = verdict
    return verdict


def eligible(entity: Entity) -> tuple[bool, str]:
    """Whether this entity may compete for a slot, and why not when it may not."""
    if not entity.name or not entity.key:
        return False, "no name"
    if not entity.ids:
        return False, "no id in the database"
    art = art_path(entity)
    if art is None:
        return False, "no icon on disk"
    if entity.kind == "npc" and screenshot_like(art):
        return False, "art is a screenshot, not a render"
    # Checked before the discount: a 3rd age piece is exactly the thing only two
    # clans have ever seen, and the whole point of the rescue is that its value
    # is the evidence its volume cannot be.
    if entity.rescued or entity.avg_value >= RESCUE_AVG_VALUE:
        entity.rescued = True
        return True, ""
    discount_unbelievable_drops(entity)
    if entity.mentions <= 0:
        return False, f"drop volume from under {MIN_DROP_GROUPS} groups"
    if entity.breadth < MIN_PLAYERS:
        return False, f"only {entity.breadth} player(s)"
    return True, ""


#: Reasons an entity was cut that a person could fix by supplying a file, as
#: opposed to reasons that are a verdict on the data. The report names these
#: individually so the fix is a short shopping list rather than a count.
FIXABLE_REASONS = ("no icon on disk", "art is a screenshot, not a render")


def select(items: dict, npcs: dict, budget: int,
           npc_share: float) -> tuple[list, list, dict, list]:
    """Fill the budget from both pools, tallying why everything else was cut.

    NPCs go first because their pool is closed and small: everything above
    :data:`NPC_FLOOR` gets in, and whatever the ceiling leaves unspent falls
    through to items, which always have more candidates than slots.

    Items are then filled in two passes. The bulk goes to the most-mentioned,
    which is what "most popularly transmitted" means. A bounded remainder goes
    to trophies that the frequency pass missed — sorting those in with everyone
    else would put a pet nobody has seen in two years above the single
    most-announced drop on the platform, which is the opposite of the ask.
    """
    rejected = defaultdict(int)
    fixable = []

    def rank(pool):
        keep = []
        for entity in pool.values():
            ok, reason = eligible(entity)
            if ok:
                keep.append(entity)
            else:
                rejected[reason] += 1
                if reason in FIXABLE_REASONS:
                    fixable.append(entity)
        keep.sort(key=lambda e: (-e.mentions, e.name))
        return keep

    # rank() both filters and tallies, so it must run once per pool — calling it
    # twice counted every rejected NPC twice in the report.
    eligible_npcs = rank(npcs)
    ranked_npcs = [e for e in eligible_npcs if e.mentions >= NPC_FLOOR]
    chosen_npcs = ranked_npcs[:int(budget * npc_share)]
    rejected[f"under {NPC_FLOOR} mentions (NPC floor)"] = len(eligible_npcs) - len(ranked_npcs)

    item_slots = budget - len(chosen_npcs)
    ranked_items = rank(items)
    reserve = int(item_slots * TROPHY_RESERVE_SHARE)
    by_volume = ranked_items[:item_slots - reserve]
    picked = {e.key for e in by_volume}
    # Rarest first: a trophy's whole claim is that volume does not measure it,
    # so spend the reserve on the ones frequency ranks worst.
    trophies = [e for e in ranked_items if e.rescued and e.key not in picked]
    trophies.sort(key=lambda e: (e.mentions, e.name))
    for entity in trophies[:reserve]:
        entity.reserved = True
    chosen_items = by_volume + trophies[:reserve]

    # Any reserve the trophies did not need goes back to the frequency ranking
    # rather than being left on the table.
    if len(chosen_items) < item_slots:
        picked = {e.key for e in chosen_items}
        for entity in ranked_items:
            if len(chosen_items) >= item_slots:
                break
            if entity.key not in picked:
                chosen_items.append(entity)
    chosen_items.sort(key=lambda e: (-e.mentions, e.name))
    # Only worth naming the ones that would have made the cut: an item nobody
    # has seen twice is not a missing-art problem.
    floor = chosen_items[-1].mentions if chosen_items else 0
    fixable = sorted((e for e in fixable if e.mentions >= floor),
                     key=lambda e: -e.mentions)
    return chosen_items, chosen_npcs, dict(rejected), fixable


def assign_names(chosen: list) -> dict:
    """``(kind, key)`` -> Discord emoji name, unique within the whole set.

    Discord allows ``[A-Za-z0-9_]{2,32}``, which 46 real item names overflow
    ("Rune scimitar ornament kit (saradomin)") and two pairs collide on
    ("Weapon poison" / "Weapon poison(++)"). Truncation is applied at a word
    boundary so the name stays readable, and a collision takes a numeric suffix
    rather than silently overwriting the earlier entry — which would leave one
    of the two pointing at the other's art.

    Keyed on kind as well as key because a clog *source* is frequently also an
    item: "Unsired", "Bird nest" and "Intricate pouch" each exist in both
    ``items`` and ``npc_list``. They collide on the key and not on the emoji
    name, which the ``item_``/``npc_`` prefixes already separate — so keying on
    the key alone silently hands the item the NPC's glyph.
    """
    taken, names = set(), {}
    for entity in chosen:
        base = emoji_name(entity.kind, entity.name)
        name = base
        if name in taken:
            for suffix in range(2, 100):
                tail = f"_{suffix}"
                name = f"{base[:32 - len(tail)]}{tail}"
                if name not in taken:
                    break
        taken.add(name)
        names[(entity.kind, entity.key)] = name
    return names


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def build_manifest(chosen_items, chosen_npcs, fanout, budget) -> dict:
    """The committed record of what the set contains and how it was chosen."""
    def entries(chosen, names):
        out = []
        for rank_index, entity in enumerate(chosen, 1):
            art = art_path(entity)
            out.append({
                "name": entity.name,
                "key": entity.key,
                "emoji": names[(entity.kind, entity.key)],
                "id": int(art.stem),
                "ids": sorted(entity.ids),
                "rank": rank_index,
                "mentions": round(entity.mentions),
                "surfaces": {k: round(v) for k, v in sorted(entity.surfaces.items())},
                "rescued": entity.rescued,
            })
        return out

    names = assign_names(chosen_items + chosen_npcs)
    return {
        "_comment": "Generated by scripts/rank_game_emojis.py — do not hand-edit; rerun the script.",
        "criteria": {
            "budget": budget,
            "min_drop_groups": MIN_DROP_GROUPS,
            "min_players": MIN_PLAYERS,
            "rescue_avg_value": RESCUE_AVG_VALUE,
            "fanout_window_days": FANOUT_WINDOW_DAYS,
            "measured_fanout": fanout,
        },
        "items": entries(chosen_items, names),
        "npcs": entries(chosen_npcs, names),
    }


def print_report(chosen_items, chosen_npcs, rejected, fixable, fanout, top) -> None:
    def table(title, chosen):
        print(f"\n=== {title}: {len(chosen)} selected ===")
        print(f"{'#':>4}  {'name':34} {'mentions':>9} {'brdth':>6}  surfaces")
        for index, e in enumerate(chosen[:top], 1):
            marker = "+" if e.reserved else ("*" if e.rescued else " ")
            surfaces = ",".join(f"{k}:{round(v)}" for k, v in sorted(e.surfaces.items()))
            print(f"{index:>4}{marker} {e.name[:34]:34} {round(e.mentions):>9} {e.breadth:>6}  {surfaces}")
        if len(chosen) > top:
            tail = chosen[-1]
            print(f"     ... {len(chosen) - top} more, down to "
                  f"{tail.name!r} at {round(tail.mentions)} mentions")

    print(f"measured notification fan-out (last {FANOUT_WINDOW_DAYS}d): {fanout}")
    table("ITEMS", chosen_items)
    table("NPCs", chosen_npcs)
    print("\n=== not selected ===")
    for reason, count in sorted(rejected.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>6}  {reason}")
    if fixable:
        print("\n=== would be in the set if they had usable art ===")
        for entity in fixable:
            # Name the file that is actually in the way: for a screenshot that
            # is the one on disk, not the lowest id, which may not exist.
            existing = art_path(entity)
            directory = NPC_ART_DIR if entity.kind == "npc" else ITEM_ART_DIR
            target = existing or (directory / f"{entity.ids_by_use()[0]}.png")
            verb = "replace" if existing else "add"
            print(f"  {round(entity.mentions):>7} mentions  {entity.name[:32]:32} "
                  f"-> {verb} {directory.name}/{target.name}")
    trophies = [e for e in chosen_items if e.rescued]
    reserved = [e for e in chosen_items if e.reserved]
    print(f"\n  {len(trophies)} of the {len(chosen_items)} items are marked *, admitted on importance "
          f"rather than volume\n  (a pet, or a drop averaging over {RESCUE_AVG_VALUE:,} gp). "
          f"{len(reserved)} of those took a reserved slot the\n  frequency pass would not have given "
          f"them; the rest earned their place on volume anyway.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--report", action="store_true", help="print the ranking, write nothing")
    parser.add_argument("--write", action="store_true", help="rewrite the manifest")
    parser.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="emoji slots to fill")
    parser.add_argument("--npc-share", type=float, default=DEFAULT_NPC_SHARE,
                        help="ceiling on the fraction of the budget NPCs may take")
    parser.add_argument("--top", type=int, default=40, help="rows to show per table in --report")
    parser.add_argument("--explain", metavar="NAME", help="show one entity's evidence and verdict")
    args = parser.parse_args()

    conn = connect()
    try:
        fanout = measure_fanout(conn)
        items, npcs = collect(conn, fanout)
    finally:
        conn.close()
    print(f"collected {len(items)} item names and {len(npcs)} NPC names from notification history")

    if args.explain:
        needle = re.sub(r"[^a-z0-9]+", "", args.explain.lower())
        hits = [e for e in list(items.values()) + list(npcs.values())
                if needle in re.sub(r"[^a-z0-9]+", "", e.name.lower())]
        if not hits:
            print(f"nothing matching {args.explain!r}")
            return 1
        for entity in sorted(hits, key=lambda e: -e.mentions)[:10]:
            ok, reason = eligible(entity)
            art = art_path(entity)
            print(f"\n{entity.kind} {entity.name!r}")
            print(f"  key       : {entity.key}")
            print(f"  emoji     : {emoji_name(entity.kind, entity.name)}")
            print(f"  ids       : {sorted(entity.ids)}")
            print(f"  art       : {art if art else 'NONE — cannot be seeded'}")
            print(f"  mentions  : {round(entity.mentions)} "
                  f"({', '.join(f'{k}={round(v)}' for k, v in sorted(entity.surfaces.items()))})")
            print(f"  breadth   : {entity.groups} groups / {entity.players} players")
            print(f"  value     : avg {entity.avg_value:,} / max {entity.max_value:,}")
            print(f"  verdict   : {'eligible' if ok else 'REJECTED — ' + reason}"
                  f"{' (admitted on importance)' if entity.rescued else ''}")
        return 0

    fetched = fetch_missing_item_art(list(items.values()))
    if fetched:
        print(f"  fetched {fetched} item icon(s) RuneLite had and this box did not")

    chosen_items, chosen_npcs, rejected, fixable = select(
        items, npcs, args.budget, args.npc_share)

    if args.write:
        manifest = build_manifest(chosen_items, chosen_npcs, fanout, args.budget)
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        print(f"wrote {len(manifest['items'])} items + {len(manifest['npcs'])} NPCs "
              f"to {MANIFEST_PATH.relative_to(PROJECT_ROOT)}")
        return 0

    print_report(chosen_items, chosen_npcs, rejected, fixable, fanout, args.top)
    if not args.report:
        print("\n(nothing written — pass --write to rewrite the manifest)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
