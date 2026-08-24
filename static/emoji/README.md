# Source art for the bot's application emojis

`scripts/seed_app_emojis.py` uploads the set defined in `utils/app_emojis.py`
to each Discord **application** (the core bot and the Hall of Fame bot are
separate apps, so each gets its own copy under its own id).

By default it takes the art straight off `cdn.discordapp.com` using the
`legacy_id` in each `Spec` — the guild emoji that key is migrating away from.
Nothing to export by hand.

**A file in this directory wins over the CDN copy.** Name it after the registry
key, e.g. `construction.png`. `.gif` is checked first, then `.png`; anything
over Discord's 256 KiB emoji limit is downscaled before upload.

Use it to:

- supply art for a key whose original guild emoji no longer exists — `join` is
  in this state today, so it renders its Unicode fallback (📥) until someone
  drops `join.png` here and reruns the seeder;
- replace art without touching the guild the emoji originally came from.

After adding a file:

```bash
./venv/bin/python scripts/seed_app_emojis.py --profile all
```

The run rewrites `static/app_emojis.json` (committed) with the new ids. Existing
emojis are reused by name, so a rerun only fills gaps — to *replace* art that is
already uploaded, delete that emoji from the application first (or use
`--prune` after removing the key from `SPECS`).
