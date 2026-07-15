# wom.py divergence

`requirements.txt` pins `wom.py==1.0.0`, which is no longer maintained at that
line — the WOM service has since added metrics (Sailing, Yama, Doom of
Mokhaiotl, …) and country codes (GB_SCT/GB_WLS) that 1.0.0's enums don't know.
Because wom.py decodes API responses with **msgspec typed decoders**, an
unknown enum value doesn't degrade gracefully — it raises and fails the whole
response decode (a single player snapshot containing an unknown metric breaks
the request). Enums also can't be extended at runtime, so the fix has to live
in the library source.

The production venv's copy of wom.py is therefore **hand-patched in
site-packages**. The canonical record of that divergence is
[`enums-divergence.patch`](enums-divergence.patch) (diff of pristine 1.0.0 →
our copy; regenerate with `diff -ru <pristine>/wom <venv>/…/wom`).

## If you rebuild the venv / reinstall requirements

`pip install -r requirements.txt` will clobber the patched copy with pristine
1.0.0. Re-apply:

```bash
cd venv/lib/python3.11/site-packages
patch -p1 --directory=wom --strip=2 < /store/droptracker/disc/deploy/wom/enums-divergence.patch
# or: patch -d venv/lib/python3.11/site-packages -p1 < deploy/wom/enums-divergence.patch
find venv/lib/python3.11/site-packages/wom -name '__pycache__' -exec rm -rf {} +
```

## When WOM adds a new metric

1. Add the enum member(s) to the venv copy (`wom/enums.py` — both the `Metric`
   class and the `SKILLS`/`BOSSES`/`ACTIVITIES` frozensets).
2. Regenerate `enums-divergence.patch` and commit it.
3. Mirror boss/skill slugs into the `_WOM_*_SLUGS` augmentation block in
   `utils/wiseoldman.py` (that keeps metric *mapping* working even on a
   pristine install, e.g. CI — CI cannot see the hand-patched venv).

## The real fix (TODO)

Maintain a fork (e.g. `DropTracker-io/wom.py`, branched from upstream
`v1.0.0`, patch applied) and pin `requirements.txt` to it:

```
wom.py @ git+https://github.com/DropTracker-io/wom.py@<sha>
```

Then CI, prod, and every fresh venv install identical, versioned code and this
directory reduces to the fork's changelog. Upstream also has a v3.x line —
migrating to it is a separate, larger task (breaking client API changes).
