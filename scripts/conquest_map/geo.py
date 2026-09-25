"""Where everything sits on the real OSRS map, in game coordinates.

Every boss is placed at the surface spot you walk to in-game to fight it
(dungeon entrance, raid lobby, lair mouth), taken from the OSRS wiki's
location pages and checked against the wiki world map. Instanced or
underground fights use their entrance; the Abyss has no surface, so it gets
an inset "rift" island of its own.

Game coordinates: x grows east, y grows north. The board flips y.
"""
from __future__ import annotations

# The slice of Gielinor the board shows (game tiles): Zeah's west coast to
# Morytania's east coast, Tombs of Amascut in the south to Ungael (Vorkath)
# in the north.
EXTENT = {"x0": 1090, "x1": 3970, "y0": 2630, "y1": 4130}

# The wiki's full world map (File:Old School RuneScape world map.png,
# 9216 x 6528): 3 px per game tile, x starts at 960 and y's top edge is 4224.
# Checked against Lumbridge castle, Varrock square, Ardougne market and
# Kourend castle.
WIKI_MAP = {"px_per_tile": 3, "x_origin": 960, "y_top": 4224}

# Regions, in display order. ``bosses`` is (key, x, y) at the boss's real
# location. God Wars shares one entrance, so its five rooms are spread down
# Troll Country from Nex's frozen prison in the north.
REGIONS = (
    {"key": "kourend", "name": "Kourend & Kebos", "color": "#b5743a", "bosses": (
        ("chambers_of_xeric", 1246, 3550),      # Mount Quidamortem
        ("alchemical_hydra", 1311, 3807),       # Mount Karuulm
        ("sarachnis", 1702, 3574),              # Forthos Dungeon
        ("vardorvis", 1162, 3379),              # The Stranglewood
        ("yama", 1435, 3668),                   # Chasm of Fire
    )},
    {"key": "varlamore", "name": "Varlamore", "color": "#d98a3d", "bosses": (
        ("fortis_colosseum", 1820, 3105),       # Civitas illa Fortis
        ("hueycoatl", 1509, 3290),              # The Darkfrost
        ("amoxliatl", 1641, 3226),              # Tower of Ascension
        ("moons_of_peril", 1441, 3186),         # Cam Torum
        ("doom_of_mokhaiotl", 1300, 3090),      # Tlati Rainforest
    )},
    {"key": "fremennik", "name": "Fremennik & Ghorrock", "color": "#5b8fbf", "bosses": (
        ("dagannoth_kings", 2525, 3743),        # Waterbirth Island
        ("vorkath", 2272, 4064),                # Ungael
        ("phantom_muspah", 2890, 3935),         # Ghorrock Dungeon
        ("duke_sucellus", 2945, 3948),          # Ghorrock Prison
    )},
    {"key": "kandarin", "name": "Kandarin & Tirannwn", "color": "#3f9a6e", "bosses": (
        ("zulrah", 2193, 3060),                 # Zul-Andra
        ("corrupted_gauntlet", 2235, 3325),     # Prifddinas
        ("kraken", 2278, 3611),                 # Kraken Cove
        ("thermonuclear_smoke_devil", 2412, 3061),  # Smoke Devil Dungeon
        ("demonic_gorillas", 2436, 3520),       # Crash Site Cavern
    )},
    {"key": "gwd", "name": "God Wars", "color": "#8a73c4", "bosses": (
        ("nex", 2893, 3868),
        ("kree_arra", 2842, 3808),
        ("general_graardor", 2932, 3775),
        ("commander_zilyana", 2848, 3712),
        ("kril_tsutsaroth", 2925, 3655),
    )},
    {"key": "wilderness", "name": "The Wilderness", "color": "#b8453a", "bosses": (
        ("callisto_artio", 3291, 3849),         # Callisto's Den
        ("vetion_calvarion", 3219, 3788),       # Vet'ion's Rest
        ("venenatis_spindel", 3319, 3780),      # Silk Chasm
        ("chaos_elemental", 3282, 3922),        # west of Rogues' Castle
        ("wilderness_demi_bosses", 3200, 3944),  # Scorpion Pit
        ("king_black_dragon", 3068, 3853),      # lever in the Lava Maze
        ("corporeal_beast", 3201, 3679),        # Graveyard of Shadows
    )},
    {"key": "heartlands", "name": "Asgarnia & Misthalin", "color": "#6aa84f", "bosses": (
        ("cerberus", 2884, 3398),               # Taverley Dungeon
        ("the_whisperer", 2998, 3494),          # Camdozaal, Ice Mountain
        ("royal_titans", 3008, 3150),           # Asgarnian Ice Dungeon
        ("scurrius", 3237, 3458),               # Varrock Sewers
        ("tormented_demon", 3169, 3172),        # Lumbridge Swamp Caves
    )},
    {"key": "morytania", "name": "Morytania", "color": "#7d6aa0", "bosses": (
        ("barrows", 3565, 3289),
        ("theatre_of_blood", 3663, 3220),       # Ver Sinhaza
        ("nightmare", 3728, 3302),              # Slepe
        ("grotesque_guardians", 3428, 3537),    # Slayer Tower
        ("araxxor", 3660, 3405),                # Morytania Spider Cave
    )},
    {"key": "desert", "name": "Kharidian Desert", "color": "#d6aa45", "bosses": (
        ("kalphite_queen", 3226, 3108),         # Kalphite Lair
        ("tombs_of_amascut", 3365, 2705),       # Necropolis
    )},
    {"key": "abyss", "name": "The Abyss", "color": "#5a4fb0", "inset": True, "bosses": (
        ("abyssal_sire", 0, 0),                 # placed on the rift island
        ("the_leviathan", 0, 0),
    )},
)

# The Abyss has no place on the surface. It is drawn as a rift island out in
# the northern sea, tethered to its real way in: the Mage of Zamorak at the
# Wilderness ditch north of Edgeville.
ABYSS = {"center": (3560, 3950), "radius": 80, "tether": (3104, 3560)}

# Each region's ground: the real kingdom's extent (game coordinates), as
# polygons. Where two overlap, the earlier region in AREA_PRIORITY wins.
# Land inside no polygon (Karamja, Feldip, the Sailing islands) joins the
# nearest kingdom, so the whole map is in play. Borders are wobbled when
# rasterised so no straight edge survives.
AREAS = {
    "kourend": [[(1080, 3345), (1990, 3345), (1990, 4140), (1080, 4140)]],
    "varlamore": [[(1080, 2620), (1990, 2620), (1990, 3345), (1080, 3345)]],
    "kandarin": [[(2120, 2995), (2830, 2995), (2830, 3570), (2560, 3570), (2560, 3690), (2120, 3690)]],
    "fremennik": [[(2120, 3690), (2560, 3690), (2560, 3570), (2700, 3570), (2700, 3890),
                   (3010, 3890), (3010, 4140), (2120, 4140)]],
    "gwd": [[(2700, 3445), (2950, 3445), (2950, 3890), (2700, 3890)]],
    "wilderness": [[(2950, 3520), (3400, 3520), (3400, 3985), (2950, 3985)]],
    "heartlands": [[(2800, 3190), (3000, 3190), (3000, 3090), (3130, 3090), (3130, 3140),
                    (3290, 3140), (3290, 3230), (3395, 3230), (3395, 3520), (2800, 3520)]],
    "morytania": [[(3395, 3140), (3980, 3140), (3980, 3620), (3395, 3620)]],
    "desert": [[(3100, 2620), (3620, 2620), (3620, 3240), (3100, 3240)]],
}
AREA_PRIORITY = ("gwd", "fremennik", "wilderness", "morytania", "heartlands", "desert",
                 "kandarin", "kourend", "varlamore")
# Medallion metrics in board units (1 unit = 1 game tile). The site draws
# each boss badge at exactly these sizes (scaled with the map), so the room
# mapgen keeps clear around a badge is the room it really takes.
BADGE = {
    "r": 36,          # medallion radius
    "font": 21,       # name scroll font size
    "char_w": 10.3,   # average RuneScape UF glyph width at that size
    "pad": 22,        # scroll padding, both sides together
    "tail": 9,        # ribbon tail beyond each end of the scroll
    "scroll_y": 52,   # scroll centre below the medallion centre
    "scroll_h": 26,
    "up": 40,         # clear space above the medallion centre
    "down": 72,       # and below it (the scroll hangs underneath)
}
REGION_FONT = 34


def badge_half_width(label: str) -> float:
    """Half the width a badge takes: its scroll, or the medallion if wider."""
    scroll = (len(label) * BADGE["char_w"] + BADGE["pad"]) / 2 + BADGE["tail"]
    return max(scroll, BADGE["r"] + 4)


def region_label_half_width(name: str) -> float:
    return len(name) * REGION_FONT * 0.28 + 12


# Tiny islands are grown to at least this radius so they stay clickable.
MIN_ISLAND = 22


# Names that fit on a medallion's scroll.
LABELS = {
    "chambers_of_xeric": "CoX", "theatre_of_blood": "ToB",
    "tombs_of_amascut": "ToA", "thermonuclear_smoke_devil": "Thermy",
    "corrupted_gauntlet": "The Gauntlet", "wilderness_demi_bosses": "Demi-bosses",
    "callisto_artio": "Callisto", "vetion_calvarion": "Vet'ion",
    "venenatis_spindel": "Venenatis", "moons_of_peril": "Moons of Peril",
    "doom_of_mokhaiotl": "Doom", "fortis_colosseum": "Colosseum",
    "demonic_gorillas": "Demonics", "grotesque_guardians": "Grotesques",
    "alchemical_hydra": "Hydra", "dagannoth_kings": "DKs",
    "general_graardor": "Graardor", "commander_zilyana": "Zilyana",
    "kril_tsutsaroth": "K'ril", "kree_arra": "Kree'arra", "the_whisperer": "Whisperer",
    "the_leviathan": "Leviathan", "phantom_muspah": "Muspah", "duke_sucellus": "Duke",
    "king_black_dragon": "KBD", "corporeal_beast": "Corp",
    "abyssal_sire": "Abyssal Sire", "tormented_demon": "Tormented Demons",
    "royal_titans": "Royal Titans", "kalphite_queen": "Kalphite Queen",
    "chaos_elemental": "Chaos Ele", "nightmare": "Nightmare",
}


def label_for(key: str) -> str:
    return LABELS.get(key) or key.replace("_", " ").title()


def all_bosses():
    for region in REGIONS:
        for key, x, y in region["bosses"]:
            yield region, key, x, y
