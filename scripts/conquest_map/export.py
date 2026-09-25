"""Export a built map (mapgen.py's JSON) as the Gielinor preset's art pack.

Writes two files:

- the preset geometry disc ships (``services/conquest_art/<name>.json``):
  per tile its badge spot and territory shape, per region its name spot and
  outline, keyed like the preset (services/task_generator.ENCOUNTERS keys);
- the terrain backdrop the website serves (a WebP under the web repo's
  ``apps/web/public/conquest/``): sea, land and scenery only. Territories,
  badges and names are drawn live on top of it by the site.

    python scripts/conquest_map/export.py gielinor.json \\
        services/conquest_art/gielinor.json \\
        ../web/apps/web/public/conquest/gielinor-v1.webp

The art is versioned by file name (``gielinor-v1``): browsers cache it
forever, so a redrawn map gets a new name rather than overwriting.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from PIL import Image

from render import render_board


def export(geom_path: str, art_json: str, webp_path: str, quality: int = 80) -> dict:
    geom = json.load(open(geom_path))
    W, H = geom["width"], geom["height"]
    art = os.path.splitext(os.path.basename(webp_path))[0]
    pack = {
        "art": art,
        "width": W,
        "height": H,
        "background": f"/conquest/{os.path.basename(webp_path)}",
        "badge": geom["badge"],
        "region_font": geom["region_font"],
        # ``label`` is the name the badge was sized for (geo.LABELS).
        "tiles": {t["key"]: {"label": t["label"], "x": t["x"], "y": t["y"], "shape": t["path"]}
                  for t in geom["tiles"]},
        "regions": {r["key"]: {"name": r["name"], "color": r["color"], "label": r["label"],
                               "shape": r["path"]} for r in geom["regions"]},
    }
    # The Abyss inset has no computed name spot: its name sits over the rift.
    ab = geom["abyss"]
    if "abyss" in pack["regions"] and not pack["regions"]["abyss"]["label"]:
        pack["regions"]["abyss"]["label"] = [ab["x"], round(ab["y"] - ab["r"] - 22, 1)]
    with open(art_json, "w") as fh:
        json.dump(pack, fh, separators=(",", ":"))
        fh.write("\n")

    svg = render_board(geom, {}, layers="terrain")
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "terrain.html")
        png = os.path.join(tmp, "terrain.png")
        with open(page, "w") as fh:
            fh.write('<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0}'
                     'svg{display:block}</style></head><body>' + svg + "</body></html>")
        subprocess.run(["nice", "/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--hide-scrollbars", "--force-device-scale-factor=1",
                        f"--window-size={W},{H}", "--virtual-time-budget=4000",
                        f"--screenshot={png}", "file://" + page],
                       check=True, capture_output=True, timeout=180)
        Image.open(png).convert("RGB").save(webp_path, "WEBP", quality=quality, method=6)
    return {"art_json": os.path.getsize(art_json), "webp": os.path.getsize(webp_path)}


if __name__ == "__main__":
    print(export(sys.argv[1], sys.argv[2], sys.argv[3]))
