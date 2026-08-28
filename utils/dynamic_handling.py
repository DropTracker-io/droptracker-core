## This file contains helper functions for dynamic handling of images and colors. file: dynamic_handling.py - Created BY Smoke [https://github.com/Varietyz/].

# Helpers for dynamic coloring and image display
from colorsys import rgb_to_hsv, hsv_to_rgb

def get_dynamic_color(image):
    """
    Analyze the image and return a color based on the most dominant hue,
    excluding pixels that are near white or black.
    
    Process:
      1. Downsize the image to speed up processing.
      2. Convert each pixel from RGB to HSV.
      3. Filter out pixels with very low saturation (i.e. nearly white/gray)
         or very low value (i.e. nearly black).
      4. Count the occurrence of each hue.
      5. If no valid hue is found, return the default yellow.
      6. Otherwise, convert the dominant hue (with full saturation and brightness)
         back to an RGB color.
    """
    # Ensure the image is in RGB mode.
    image = image.convert('RGB')
    # Downsize for performance.
    small_img = image.resize((100, 100))
    pixels = list(small_img.getdata())

    hue_counts = {}
    # Define thresholds for saturation and brightness
    MIN_SATURATION = 0.3  # Ignore unsaturated (grayish/white) pixels.
    MIN_VALUE = 0.2       # Ignore very dark pixels.

    for r, g, b in pixels:
        h, s, v = rgb_to_hsv(r / 255, g / 255, b / 255)
        # Skip pixels that are nearly white/gray (unsaturated) or very dark.
        if s < MIN_SATURATION or v < MIN_VALUE:
            continue
        hue = int(h * 360)
        hue_counts[hue] = hue_counts.get(hue, 0) + 1

    if not hue_counts:
        # If no dominant hue is found, default to yellow.
        return (175, 175, 175)

    dominant_hue = max(hue_counts, key=hue_counts.get)
    # For intense blue and purple hues, reduce saturation and increase brightness.
    if 210 <= dominant_hue <= 330:
        adjusted_s = 0.5   # Lower saturation for blue/purple.
        adjusted_v = 0.9   # Increase brightness for better contrast.
    else:
        adjusted_s = 0.8
        adjusted_v = 1

    r, g, b = hsv_to_rgb(dominant_hue / 360, adjusted_s, adjusted_v)
    return (int(r * 255), int(g * 255), int(b * 255))

COINS_ITEM_ID = 995

#: Fallback coin thresholds for a box with no generated catalogue. These are the
#: game's real switch points, read from the cache: the hand-written table this
#: replaces had 10 -> 1000, 50 -> 1001 and 100 -> 1002, so a stack of 100 coins
#: was drawn with the 250-pile sprite and a stack of 10 with the 25-pile.
_COIN_FALLBACK = [
    (2, 996), (3, 997), (4, 998), (5, 999), (25, 1000),
    (100, 1001), (250, 1002), (1000, 1003), (10000, 1004),
]


def get_coin_image_id(quantity):
    """The coin pile sprite id for ``quantity`` coins.

    Thin wrapper over :func:`utils.item_catalogue.stack_display_id`, which reads
    the thresholds the game itself uses. Kept as a named function because the
    loot board generators call it directly and coins are the one stackable they
    special-case.
    """
    from utils.item_catalogue import stack_display_id

    resolved = stack_display_id(COINS_ITEM_ID, quantity)
    if resolved != COINS_ITEM_ID:
        return resolved
    # No catalogue on this box (or a quantity below the first threshold).
    try:
        count = int(quantity)
    except (TypeError, ValueError):
        return COINS_ITEM_ID
    display = COINS_ITEM_ID
    for threshold, variant_id in _COIN_FALLBACK:
        if count >= threshold:
            display = variant_id
        else:
            break
    return display

def get_stacked_display_id(item_id, session):
    """Resolve an OSRS item id to the icon id that best represents a *stack* of it.

    Stackable items are submitted/stored as their single-unit id, but the game
    client swaps to progressively larger "pile" graphics as the stack grows.
    Each pile graphic is a distinct item id sharing the base item's name, with
    the DB ``stacked`` column holding the count threshold it represents (this is
    how coins 995..1004 and Zulrah's scales 12934/15323/3993..3999 are stored).
    Loot leaderboards look best showing the fullest pile, so for a stackable item
    we return the largest non-noted variant sharing its name — "landing on the
    largest stack size for most cases" (suggestion #44).

    Coins are intentionally left untouched here: their pile is chosen by
    magnitude via :func:`get_coin_image_id` at the call site, which passes the
    already-resolved coin id. Returns the original id for non-stackable items,
    coins, unknown ids, or when no better variant exists.
    """
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return item_id
    # The game cache knows the real variant table, so prefer it and skip the
    # name-matching heuristic below entirely. Same thresholds as
    # get_coin_image_id now reads — one source of truth for what a stack
    # variant is, even though the two callers want different policies (fullest
    # pile here, quantity-matched there).
    #
    # Coins stay excluded, as they were before: their pile is picked by
    # magnitude at the call site, and answering "the fullest pile" here would
    # draw every coin drop as 10,000 coins.
    if item_id != COINS_ITEM_ID:
        from utils.item_catalogue import largest_stack_id

        catalogued = largest_stack_id(item_id)
        if catalogued is not None:
            return catalogued
    # Local import keeps this module light and avoids import-time DB coupling.
    from db.models import ItemList
    item = session.query(ItemList).filter(ItemList.item_id == item_id).first()
    if item is None or not item.stackable or item.item_name == "Coins":
        return item_id
    variants = (
        session.query(ItemList.item_id, ItemList.stacked)
        .filter(ItemList.item_name == item.item_name, ItemList.noted.is_(False))
        .all()
    )
    if not variants:
        return item_id
    # Prefer the greatest stack-size threshold; break ties on the higher id
    # (the "final non-noted item id available", per the suggestion).
    best_id, best_stacked = max(variants, key=lambda v: (v[1] or 0, v[0]))
    # Only swap when genuine larger-pile art exists (thresholds of 2+, as with
    # arrows, bolts, seeds and Zulrah's scales). Items whose only same-named
    # alternates are stacked 0/1 duplicates (runes, cannonballs, most drops)
    # render identically at every size, so leave them on their submitted id.
    if (best_stacked or 0) < 2:
        return item_id
    return best_id

def get_value_color(numCoins):
    """
    Return a color based on the coin value thresholds:
      - If numCoins >= 1,000,000,000, return (102, 152, 255)  [Hex 0x6698FF]
      - Else if numCoins >= 10,000,000, return (0, 255, 128)     [Hex 0x00FF80]
      - Else if numCoins >= 100,000, return (255, 255, 255)        [Hex 0xFFFFFF]
      - Else if numCoins > 0, return (255, 255, 0)                 [Hex 0xFFFF00]
      - Else, return (255, 0, 0)                                  [Hex 0xFF0000]
    """
    if numCoins >= 1_000_000_000:
        return (102, 152, 255) # OSRS Billions Blue
    elif numCoins >= 10_000_000:
        return (0, 255, 128) # OSRS Millions Green
    elif numCoins >= 100_000:
        return (255, 255, 255) # OSRS 100K White
    elif numCoins > 0:
        return (255, 255, 0) # OSRS Standard Yellow 
    else:
        return (255, 0, 0) # No Value Red

