"""Export the server-loot npc id list as ``server_loot_npc_ids.txt``.

Some NPCs award their loot **server-side**: Jagex runs the
``LOOTTRACKER_ADD_LOOT`` script and RuneLite republishes it as a
``ServerNpcLoot`` event. Nothing spawns on the death tile, so RuneLite's
client-side ground scrape (``NpcLootReceived``) never fires for them.

The plugin has to know which npcs those are, because the two RuneLite loot
events are independent and never cross-suppress each other — for an npc where
*both* fire, each one would become a separate submission, notification and
leaderboard increment. So the id list does double duty: trust the server event,
and suppress the client-side one.

Historically that list was hardcoded in the plugin
(``NpcUtilities.SERVER_LOOT_NPC_IDS``), which meant every new server-loot boss
needed a plugin release + Plugin Hub review before its drops tracked at all —
and the failure was silent (kills and PBs kept working, only loot vanished).
Mad Angel released 2026-07-29 and logged 179 personal bests against 0 drops
before anyone noticed.

Publishing the list here instead means a new boss is tracked within one publish
cycle, no plugin release involved. The plugin unions this list with its
compiled-in copy, so an unreachable/garbage file degrades to the old behaviour
rather than breaking loot tracking. This reaches **both API and non-API
(webhook-only) users** — the plugin fetches it straight from GitHub Pages via
``DropTrackerApi.fetchItemIdList``, which is not gated on ``useApi()``.

Published automatically by ``utils/github.py``
(``GithubPagesUpdater._item_list_contents``), blob-sha change-gated. To add an
npc: append it below, then let the publisher run (or run it manually).

Run:
    venv/bin/python -m scripts.export_server_loot_npcs                      # print to stdout
    venv/bin/python -m scripts.export_server_loot_npcs -o server_loot_npc_ids.txt
"""
from __future__ import annotations

import argparse

# (npc_id, npc name) — names are documentation only; the export is ids.
#
# Must stay a superset of the plugin's compiled-in NpcUtilities.SERVER_LOOT_NPC_IDS
# so that clients which cannot reach GitHub Pages behave identically.
SERVER_LOOT_NPCS = [
    (14176, "Yama"),                       # NpcID.YAMA
    (8583, "Hespori"),                     # NpcID.HESPORI
    # Sailing sea creatures — all award loot server-side on the "dead" npc.
    (15195, "Bull shark (dead)"),
    (15197, "Hammerhead shark (dead)"),
    (15199, "Tiger shark (dead)"),
    (15201, "Great white shark (dead)"),
    (15203, "Narwhal (dead)"),
    (15205, "Orca (dead)"),
    (15207, "Pygmy kraken (dead)"),
    (15209, "Spined kraken (dead)"),
    (15211, "Armoured kraken (dead)"),
    (15213, "Vampyre kraken (dead)"),
    (15215, "Eagle ray (dead)"),
    (15217, "Butterfly ray (dead)"),
    (15219, "Stingray (dead)"),
    (15221, "Manta ray (dead)"),
    (15223, "Osprey (dead)"),
    (15225, "Albatross (dead)"),
    (15227, "Frigatebird (dead)"),
    (15229, "Tern (dead)"),
    (15231, "Sea mogre (dead)"),
    (15235, "Dolphin (dead)"),
    (15577, "Veiled kraken (dead)"),
    (15742, "Maggot King"),                # NpcID.MAGGOT_KING
    (15741, "Maggot King (corpse)"),       # NpcID.MAGGOT_KING_CORPSE
    # Mad Angel (Wyrmscraig, released 2026-07-29). Four ids: the version fought
    # during Fallen From Grace and the repeatable post-quest encounter.
    (16309, "Mad Angel (quest)"),
    (16315, "Mad Angel (quest)"),
    (16305, "Mad Angel (post-quest)"),
    (16314, "Mad Angel (post-quest)"),
]


def build_content() -> str:
    """The published file body: a sorted, de-duplicated comma-separated id list.

    Sorted + de-duplicated so the output is deterministic — the publisher
    change-gates on a blob sha, and unstable ordering would commit (and trigger
    a Pages rebuild) on every run.
    """
    ids = sorted({int(npc_id) for npc_id, _name in SERVER_LOOT_NPCS})
    return ",".join(str(i) for i in ids)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", help="write to this file instead of stdout")
    args = parser.parse_args()

    content = build_content()
    if args.output:
        with open(args.output, "w") as f:
            f.write(content)
        print(f"wrote {len(SERVER_LOOT_NPCS)} npc ids to {args.output}")
    else:
        print(content)


if __name__ == "__main__":
    main()
