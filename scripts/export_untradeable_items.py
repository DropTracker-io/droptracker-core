"""Export the curated notable-untradeables id list as ``untradeable_items.txt``.

The RuneLite plugin reads a comma-separated id list from
``https://droptracker-io.github.io/content/untradeable_items.txt`` to decide
which 0gp drops to screenshot when the player's "Screenshot untradeables"
config is enabled (see ``DropTrackerApi.getNotableUntradeables`` /
``DropHandler``). Unlike ``valued_items.txt`` (override components, always
screenshotted), this list is toggle-gated client-side.

Curated 2026-07-22 from 90 days of zero-value drop data cross-referenced with
collection-log submissions (``docs/zero_value_notable_items_2026-07-22.tsv``).
Excluded on purpose: GE-tradeable items (their 0gp rows are transient price
lookup failures), pets (their ``pet`` submissions carry their own screenshot
config), and high-rate farming byproducts (>5 drops/player/month — granite
dust, abyssal pearls, satchels and the like) that would spam screenshot
uploads. Edit the list below and re-publish to the content repo to change
what clients capture.

The content repo is **not** on this box, so this prints/writes the file
content for you to commit + push to
``droptracker-io.github.io/content/untradeable_items.txt``.

Run:
    venv/bin/python -m scripts.export_untradeable_items                 # print to stdout
    venv/bin/python -m scripts.export_untradeable_items -o untradeable_items.txt
"""
from __future__ import annotations

import argparse

# (item_id, item name) — names are documentation only; the export is ids.
NOTABLE_UNTRADEABLES = [
    # Activity-tracking sources (plugin >= 5.5.0 synthetic drops): screenshot
    # the pyramid top and the trawling trophy fish, all 0gp untradeables.
    (6970, "Pyramid top"),
    (31408, "Giant blue krill"),
    (31412, "Golden haddock"),
    (31416, "Orangefin"),
    (31420, "Huge halibut"),
    (31424, "Purplefin"),
    (31428, "Swift marlin"),
    (5553, "Rogue top"),
    (6665, "Mudskipper hat"),
    (6666, "Flippers"),
    (6798, "Earth warrior champion scroll"),
    (6799, "Ghoul champion scroll"),
    (6800, "Giant champion scroll"),
    (6801, "Goblin champion scroll"),
    (6802, "Hobgoblin champion scroll"),
    (6803, "Imp champion scroll"),
    (6804, "Jogre champion scroll"),
    (6805, "Lesser demon champion scroll"),
    (6806, "Skeleton champion scroll"),
    (6807, "Zombie champion scroll"),
    (7536, "Fresh crab claw"),
    (7538, "Fresh crab shell"),
    (7976, "Cockatrice head"),
    (7977, "Basilisk head"),
    (7978, "Kurask head"),
    (7979, "Abyssal head"),
    (7980, "Kbd heads"),
    (7981, "Kq head"),
    (8844, "Bronze defender"),
    (8845, "Iron defender"),
    (8846, "Steel defender"),
    (8847, "Black defender"),
    (8848, "Mithril defender"),
    (8849, "Adamant defender"),
    (8850, "Rune defender"),
    (9007, "Right skull half"),
    (9008, "Left skull half"),
    (9010, "Top of sceptre"),
    (9011, "Bottom of sceptre"),
    (10877, "Plain satchel"),
    (10880, "Black satchel"),
    (10881, "Gold satchel"),
    (10882, "Rune satchel"),
    (10933, "Lumberjack boots"),
    (10939, "Lumberjack top"),
    (10940, "Lumberjack legs"),
    (10941, "Lumberjack hat"),
    (11338, "Chewed bones"),
    (11341, "Ancient page"),
    (11942, "Ecumenical key"),
    (12854, "Flamtaer bag"),
    (12954, "Dragon defender"),
    (13200, "Tanzanite mutagen"),
    (13201, "Magma mutagen"),
    (13258, "Angler hat"),
    (13259, "Angler top"),
    (13260, "Angler waders"),
    (13261, "Angler boots"),
    (13273, "Unsired"),
    (13357, "Shayzien gloves (1)"),
    (13358, "Shayzien boots (1)"),
    (13359, "Shayzien helm (1)"),
    (13360, "Shayzien greaves (1)"),
    (13361, "Shayzien platebody (1)"),
    (13362, "Shayzien gloves (2)"),
    (13363, "Shayzien boots (2)"),
    (13364, "Shayzien helm (2)"),
    (13365, "Shayzien greaves (2)"),
    (13366, "Shayzien platebody (2)"),
    (13367, "Shayzien gloves (3)"),
    (13368, "Shayzien boots (3)"),
    (13369, "Shayzien helm (3)"),
    (13370, "Shayzien greaves (3)"),
    (13371, "Shayzien platebody (3)"),
    (13372, "Shayzien gloves (4)"),
    (13373, "Shayzien boots (4)"),
    (13374, "Shayzien helm (4)"),
    (13375, "Shayzien greaves (4)"),
    (13376, "Shayzien platebody (4)"),
    (13377, "Shayzien gloves (5)"),
    (13378, "Shayzien boots (5)"),
    (13379, "Shayzien helm (5)"),
    (13380, "Shayzien greaves (5)"),
    (13381, "Shayzien body (5)"),
    (13392, "Xeric's talisman (inert)"),
    (19677, "Ancient shard"),
    (19679, "Dark totem base"),
    (19681, "Dark totem middle"),
    (19683, "Dark totem top"),
    (19685, "Dark totem"),
    (20704, "Pyromancer garb"),
    (20706, "Pyromancer robe"),
    (20708, "Pyromancer hood"),
    (20710, "Pyromancer boots"),
    (20712, "Warm gloves"),
    (20720, "Bruma torch"),
    (21027, "Dark relic"),
    (21275, "Dark claw"),
    (22374, "Mossy key"),
    (22386, "Metamorphic dust"),
    (22875, "Hespori seed"),
    (22969, "Hydra's heart"),
    (22971, "Hydra's fang"),
    (22973, "Hydra's eye"),
    (23077, "Alchemical hydra heads"),
    (23866, "Crystal shards"),
    (24670, "Twisted ancestral colour kit"),
    (25434, "Zealot's robe top"),
    (25436, "Zealot's robe bottom"),
    (25438, "Zealot's helm"),
    (25440, "Zealot's boots"),
    (25474, "Tree wizards' journal"),
    (25476, "Bloody notes"),
    (25539, "Celestial ring (uncharged)"),
    (25559, "Big harpoonfish"),
    (25580, "Tackle box"),
    (25582, "Fish barrel"),
    (25639, "Barronite guard"),
    (25742, "Holy ornament kit"),
    (25744, "Sanguine ornament kit"),
    (25746, "Sanguine dust"),
    (25837, "Slepey tablet"),
    (25838, "Parasitic egg"),
    (25844, "Orange egg sac"),
    (25846, "Blue egg sac"),
    (26807, "Abyssal green dye"),
    (26809, "Abyssal blue dye"),
    (26811, "Abyssal red dye"),
    (26813, "Abyssal needle"),
    (26822, "Abyssal lantern"),
    (26908, "Intricate pouch"),
    (26910, "Tarnished locket"),
    (26912, "Lost bag"),
    (27248, "Cursed phalanx"),
    (27255, "Menaphite ornament kit"),
    (27279, "Thread of elidinis"),
    (27283, "Breach of the scarab"),
    (27285, "Eye of the corruptor"),
    (27289, "Jewel of the sun"),
    (27293, "Cache of runes"),
    (27372, "Masori crafting kit"),
    (27377, "Remnant of akkha"),
    (27378, "Remnant of ba-ba"),
    (27379, "Remnant of kephri"),
    (27380, "Remnant of zebak"),
    (27381, "Ancient remnant"),
    (27622, "Frozen cache"),
    (27627, "Ancient icon"),
    (27643, "Charged ice"),
    (28268, "Blood quartz"),
    (28270, "Ice quartz"),
    (28272, "Shadow quartz"),
    (28274, "Smoke quartz"),
    (28330, "Strangled tablet"),
    (28331, "Sirenic tablet"),
    (28332, "Scarred tablet"),
    (28333, "Frozen tablet"),
    (28947, "Dizana's quiver (uncharged)"),
    (29263, "Guild hunter headwear"),
    (29265, "Guild hunter top"),
    (29267, "Guild hunter legs"),
    (29269, "Guild hunter boots"),
    (29309, "Huntsman's kit"),
    (29781, "Coagulated venom"),
    (29786, "Jar of Venom"),
    (29788, "Araxyte head"),
    (29892, "Pendant of ates (inert)"),
    (30626, "Deadeye prayer scroll"),
    (30627, "Mystic vigour prayer scroll"),
    (30637, "Giantsoul amulet (uncharged)"),
    (30763, "Forgotten lockbox"),
    (30806, "Rite of vile transference"),
    (30893, "Jewel of amascut"),
    (30902, "Minor beginner scroll case"),
    (30904, "Major beginner scroll case"),
    (30906, "Minor easy scroll case"),
    (30908, "Major easy scroll case"),
    (30910, "Minor medium scroll case"),
    (30912, "Major medium scroll case"),
    (30914, "Minor hard scroll case"),
    (30916, "Major hard scroll case"),
    (30918, "Minor elite scroll case"),
    (30920, "Major elite scroll case"),
    (30922, "Minor master scroll case"),
    (30924, "Major master scroll case"),
    (30926, "Mimic scroll case"),
    (31043, "Fletching knife"),
    (31052, "Bow string spool"),
    (31084, "Alchemist's signet"),
    (32113, "Sandy paint"),
    (32398, "Sailors' amulet (inert)"),
    (32863, "Rusty locket"),
    (32864, "Mouldy block"),
    (32865, "Dull knife"),
    (32866, "Broken compass"),
    (32867, "Rusty coin"),
    (32868, "Broken sextant"),
    (32869, "Mouldy doll"),
    (32870, "Smashed mirror"),
    (32921, "Jar of feathers"),
    (33133, "Pristine spider silk"),
    (33382, "Immaculate mole skin"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", help="Write to this file instead of stdout.")
    args = ap.parse_args()

    ids = sorted({int(item_id) for item_id, _name in NOTABLE_UNTRADEABLES})
    txt = ",".join(str(i) for i in ids)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(f"Wrote {len(ids)} ids to {args.output}")
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
