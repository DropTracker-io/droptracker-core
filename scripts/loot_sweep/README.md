# Loot Sweep (All Content) template

Generates + seeds the global **"Loot Sweep (All Content)"** event template from
the balancing spreadsheet. Built for the `loot_sweep` v2 model (nested groups,
NPC-scoped items, batched decay — see `docs/LOOT_SWEEP.md`).

## Files

| File | What |
|---|---|
| `all_content_sheet.csv` | The source sheet (CSV export). |
| `build_from_sheet.py` | Parses the sheet → `all_content_template.json` + `REVIEW.md`. Resolves item + NPC names against the DB, folds meta-sets (Barrows / Dagannoth Kings / Moons), and collapses the sheet's duplicate `(1,3,5,7,9)` rows into one item with `awards_per_tier`. |
| `all_content_template.json` | The generated template payload (48 loot_sweep tasks). |
| `REVIEW.md` | **Read this** — everything the generator skipped or couldn't resolve. |
| `seed_template.py` | Re-validates every task, then (with `--commit`) inserts the global template. |

## Status — needs review before seeding

48 tasks generated. Not yet seeded. Open items (`REVIEW.md`):

- **Pets (37) are excluded.** loot_sweep items only credit from a *drop*
  submission carrying the source NPC; pets arrive as `pet` submissions, so they
  can't score as loot_sweep items as-is. Decide how you want pet bonuses handled.
- **8 sets have no source NPC** (Wilderness wards, Revenant Caves, Virtus /
  Obsidian sets, Theater of Blood, Slayer non-Boss, Miscellaneous I/II) — these
  aren't a single NPC. Until you set NPCs, their items match ANY NPC. Fix in the
  JSON (or the admin editor once seeded).
- **21 item names** need the canonical spelling (heads, mutagens, raid
  dust/kits, champion scrolls, `pendant of ates`).
- **Champion Scrolls + Bounty challenge #1-5** were skipped (multi-NPC / manual).

## Regenerate

```bash
python scripts/loot_sweep/build_from_sheet.py     # needs DB creds in .env
```

## Seed (prod write — deliberate)

```bash
python -m scripts.loot_sweep.seed_template          # dry-run: validate only
python -m scripts.loot_sweep.seed_template --commit # insert the global template
```

`--commit` inserts one `web_event_templates` row (`group_id` NULL, `visibility`
public, kind loot_sweep) — visible to every group's template picker. Only
creatable while `loot_sweep` stays `admin_only` by superadmins / test-group
clans (the instantiate gate). Enable it in `/admin/event-types` to open it up.
