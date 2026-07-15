"""Optional real-sprite support.

By default decorations are drawn as the vector glyphs in icons.py. To use real
OSRS-style art instead, drop PNG/SVG files in assets/ and describe them in
assets/manifest.json, e.g.:

    {
      "icon_px": 64,
      "icons": {
        "tree":  "sprites/tree.png",
        "anvil": "sprites/anvil.png",
        "crystal": "sprites/crystal.svg"
      }
    }

Then call icons.set_sprite_library(assets.load()) once before rendering; any
icon named in the manifest is emitted as an <image> (keeping it on its own,
editable layer element) and everything else falls back to the vector glyph.
Nothing in board.py / render.py needs to change.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_DIR = os.path.join(HERE, "assets")


class SpriteLibrary:
    def __init__(self, sprites: dict[str, str], embed: bool = True):
        self.sprites = sprites          # icon name -> absolute file path
        self.embed = embed

    def has(self, name: str) -> bool:
        return name in self.sprites

    def href(self, name: str) -> str:
        path = self.sprites[name]
        if not self.embed:
            return os.path.relpath(path, ASSET_DIR)
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        return f"data:{mime};base64,{data}"


def load(manifest_path: str | None = None, embed: bool = True) -> SpriteLibrary | None:
    manifest_path = manifest_path or os.path.join(ASSET_DIR, "manifest.json")
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, encoding="utf-8") as fh:
        data = json.load(fh)
    sprites = {}
    for name, rel in data.get("icons", {}).items():
        p = rel if os.path.isabs(rel) else os.path.join(ASSET_DIR, rel)
        if os.path.exists(p):
            sprites[name] = p
    return SpriteLibrary(sprites, embed=embed) if sprites else None
