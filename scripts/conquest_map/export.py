"""Export a built map (mapgen.py's JSON) as the Gielinor preset's art pack.

Writes two files:

- the preset geometry disc ships (``services/conquest_art/<name>.json``):
  per tile its badge spot and territory shape, per region its name spot and
  outline, keyed like the preset (services/conquest_presets tile keys), and
  which territories border each other (``edges``, the fronts rule);
- the terrain backdrop the website serves (a WebP under the web repo's
  ``apps/web/public/conquest/``): sea, land and scenery only. Territories,
  badges and names are drawn live on top of it by the site.

    python scripts/conquest_map/export.py gielinor.json \\
        services/conquest_art/gielinor.json \\
        ../web/apps/web/public/conquest/gielinor-v3.webp

The art is versioned by file name (``gielinor-v3``): browsers cache it
forever, so a redrawn map gets a new name rather than overwriting. Keep the
old WebPs: maps built from an earlier version still point at theirs (their
territory outlines are copied into their own rows).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from PIL import Image

from render import render_board


def export(geom_path, art_json: str, webp_path: str, quality: int = 80) -> dict:
    """``geom_path``: mapgen's JSON (a path, or the dict itself)."""
    geom = json.load(open(geom_path)) if isinstance(geom_path, str) else geom_path
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
                               "shape": r["path"],
                               **({"sea": True, "labels": r.get("labels") or {}}
                                  if r.get("sea") else {})}
                    for r in geom["regions"]},
        "edges": geom["edges"],
        "selection": geom.get("selection"),
    }
    # The Abyss inset has no computed name spot: its name sits over the rift.
    ab = geom.get("abyss")
    if ab and "abyss" in pack["regions"] and not pack["regions"]["abyss"]["label"]:
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
        # Its own profile and a writable HOME: under systemd (the web API runs
        # this for regenerated maps) the service user's home may be read-only.
        profile = os.path.join(tmp, "profile")
        subprocess.run(["nice", "/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--hide-scrollbars", "--force-device-scale-factor=1",
                        f"--user-data-dir={profile}",
                        f"--window-size={W},{H}", "--virtual-time-budget=4000",
                        f"--screenshot={png}", "file://" + page],
                       check=True, capture_output=True, timeout=180,
                       env={**os.environ, "HOME": tmp})
        Image.open(png).convert("RGB").save(webp_path, "WEBP", quality=quality, method=6)
    return {"art_json": os.path.getsize(art_json), "webp": os.path.getsize(webp_path)}


if __name__ == "__main__":
    print(export(sys.argv[1], sys.argv[2], sys.argv[3]))
