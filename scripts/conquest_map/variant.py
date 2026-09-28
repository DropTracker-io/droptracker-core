"""Regenerate the Gielinor board for one selection of regions and tiles.

The live side of the preset (services/conquest_mapgen.py) runs this as a
niced subprocess when an organiser leaves regions or tiles out: the whole
board is rebuilt without them (mapgen.build), then exported like the
committed art pack (export.export): the geometry JSON and the terrain WebP.

    python variant.py WORLD_PNG OUT_JSON OUT_WEBP --regions a,b --drop x,y

Needs the same private toolchain as mapgen.py (scipy, scikit-image and
Pillow on PYTHONPATH) and the box's headless Chromium.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import export  # noqa: E402
import mapgen  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("world")
    ap.add_argument("out_json")
    ap.add_argument("out_webp")
    ap.add_argument("--regions", default="")
    ap.add_argument("--drop", default="")
    args = ap.parse_args()
    geom = mapgen.build(args.world,
                        regions=[k for k in args.regions.split(",") if k] or None,
                        drop_tiles=[k for k in args.drop.split(",") if k])
    sizes = export.export(geom, args.out_json, args.out_webp)
    print(json.dumps({"tiles": len(geom["tiles"]), "edges": len(geom["edges"]), **sizes}))


if __name__ == "__main__":
    main()
