"""Biome palettes and decoration tables (OSRS-flavoured regions).

Each biome defines fill colours (top/bottom for a subtle bevel gradient),
an edge colour, and a weighted table of decoration icons that can appear on
its non-path tiles only (board._decorate never places icons on path tiles).
Icon names map to glyph drawers in icons.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Biome:
    key: str
    name: str
    fill_top: str
    fill_bottom: str
    edge: str
    # (icon_name, weight) — weight 0 means "never spontaneously".
    decor: list[tuple[str, float]] = field(default_factory=list)
    # Relative chance (0..1) that any given filled tile spawns a decoration.
    decor_density: float = 0.35


# Region seed order roughly mirrors the reference maps: start at Tutorial,
# tour outward, end in the far reaches.
BIOMES: dict[str, Biome] = {
    "tutorial": Biome(
        "tutorial", "Tutorial", "#9298a2", "#6f757e", "#474b52",
        decor=[("tree", 1)], decor_density=0.25),
    "misthalin": Biome(
        "misthalin", "Misthalin", "#5fa843", "#3f7a2b", "#28511b",
        decor=[("tree", 5), ("mushroom", 1)]),
    "asgarnia": Biome(
        "asgarnia", "Asgarnia", "#cf9a34", "#a06f1e", "#6d4a12",
        decor=[("anvil", 4)]),
    "wilderness": Biome(
        "wilderness", "Wilderness", "#b6403f", "#7d2626", "#4d1616",
        decor=[("skull", 3), ("deadtree", 3)], decor_density=0.5),
    "fremennik": Biome(
        "fremennik", "Fremennik", "#e2eaf1", "#bcc9d5", "#8fa0b0",
        decor=[("snowtree", 4)]),
    "desert": Biome(
        "desert", "Desert", "#e5c552", "#c39f2f", "#8a6f1c",
        decor=[("cactus", 5), ("skull", 1)]),
    "kandarin": Biome(
        "kandarin", "Kandarin", "#c95fa0", "#9c3f78", "#66284d",
        decor=[("tree", 4), ("mushroom", 3)]),
    "karamja": Biome(
        "karamja", "Karamja", "#41913f", "#2b5f2b", "#1a3d1a",
        decor=[("tree", 5), ("mushroom", 2)], decor_density=0.45),
    "morytania": Biome(
        "morytania", "Morytania", "#556b52", "#37472f", "#212d1c",
        decor=[("deadtree", 4), ("skull", 3), ("mushroom", 2)], decor_density=0.5),
    "tirannwyn": Biome(
        "tirannwyn", "Tirannwyn", "#aecbe4", "#82a6c6", "#5a7d9c",
        decor=[("crystal", 5), ("tree", 1)]),
    "fossil": Biome(
        "fossil", "Fossil Island", "#4c916d", "#2f6b4a", "#1c4630",
        decor=[("fossil", 4), ("tree", 2)]),
    "varlamore": Biome(
        "varlamore", "Varlamore", "#d98a4f", "#a5602c", "#6b3c16",
        decor=[("tree", 3), ("crystal", 2), ("chest", 2)]),
}

# Default tour order used to lay region seeds start -> finish. Tutorial always
# opens the tour and Varlamore always closes it (board._place_regions pins
# both regardless of which middle regions/count are chosen).
DEFAULT_TOUR = [
    "tutorial", "misthalin", "karamja", "asgarnia", "kandarin",
    "fremennik", "wilderness", "desert", "morytania", "tirannwyn", "fossil",
    "varlamore",
]

# Glow colours for the start/finish tiles (the only ones ever glowed).
GLOW_COLORS = {
    "start": "#7fe08a",
    "finish": "#ffd35a",
}


def biome(key: str) -> Biome:
    return BIOMES[key]
