"""Export active override item ids as the GitHub Pages ``valued_items.txt``.

The RuneLite plugin reads a comma-separated id list from
``https://droptracker-io.github.io/content/valued_items.txt`` to decide which
0gp drops to screenshot / ask the server to re-value (see
``DropTrackerApi.getValuedUntradeables``). This regenerates that list from the
``item_value_overrides`` table so it stays in sync with the valuation rules.
Name-only override rows (``item_id`` NULL, matched by name at intake) are
resolved to their items-table ids — see ``utils.value_overrides.active_item_ids``.

The content repo is **not** on this box, so this prints/writes the file content
for you to commit + push to ``droptracker-io.github.io/content/valued_items.txt``
(the ``GET /api/v1/admin/item-values/export`` endpoint returns the same string
for copy-paste from the dashboard). Alternatively point the plugin's API path at
``GET /value_mods``, which already serves this list live.

Run:
    venv/bin/python -m scripts.export_valued_items                 # print to stdout
    venv/bin/python -m scripts.export_valued_items -o valued_items.txt
"""
from __future__ import annotations

import argparse
import sys

from utils.value_overrides import active_item_ids as _active_ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", help="Write to this file instead of stdout.")
    args = ap.parse_args()

    ids = _active_ids()
    if not ids:
        print("No active overrides found — is the table seeded?", file=sys.stderr)
    txt = ",".join(str(i) for i in ids)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print(f"Wrote {len(ids)} ids to {args.output}", file=sys.stderr)
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
