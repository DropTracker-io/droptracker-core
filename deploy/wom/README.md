# wom.py

We depend on a **fork** of wom.py, not PyPI:

```
# requirements.txt
wom.py @ git+https://github.com/DropTracker-io/wom.py@<sha>
```

Repo: https://github.com/DropTracker-io/wom.py (branch `droptracker`, forked
from upstream 1.0.0). Fork rationale + change details: `DROPTRACKER_FORK.md` in
that repo.

## Why a fork (the problem it solves)

Upstream `wom.py==1.0.0` is unmaintained at that line, and it decodes API
responses with **msgspec typed decoders**. Metric-bearing fields were typed as
the strict `Metric` enum, so a single metric the client didn't know (a boss/skill
WOM added after 1.0.0) failed the **entire** response decode — one unknown boss
in one player's snapshot broke `get_details` / `update_player` / `get_gains`.
Enums can't be extended at runtime, so the fix has to live in the library, and
prod used to hand-patch `site-packages` (invisible, clobbered on every
`pip install`, and diverged from CI — which is exactly how CI failed 3× on
2026-07-15).

The fork fixes this structurally: metric-bearing **response** fields are typed
`MetricValue` (`== str`), so unknown metrics pass through as their slug string
instead of raising. **New game content no longer breaks anything** — no crash,
no outage, no CI/prod skew.

## Maintenance — now minimal

- **Fresh venv / reinstall:** `pip install -r requirements.txt` pulls the fork.
  Nothing to hand-apply. (This directory used to hold an `enums-divergence.patch`
  to re-apply after every reinstall — that footgun is gone.)
- **WOM adds a new boss/skill:** nothing is on fire — decoding tolerates it.
  When you want WOM-hybrid event tracking to *recognize* the new metric (so a
  `kc_target`/`skill_target` event task reconciles via WOM instead of
  plugin-only), add the enum member in the fork's `wom/enums.py`, push, and bump
  the `@<sha>` pin here. `utils/wiseoldman.py` derives its slug sets straight
  from the enum, so there's no second list to update. The events reconciler logs
  when a task target has no WOM metric, so gaps surface on their own.
- **Bumping the pin on an EXISTING venv — use `--force-reinstall`.** The fork's
  version string never changes (always `1.0.0+dt.1`), so `pip install -r
  requirements.txt` (even `--upgrade`) sees "same version already installed" and
  **silently no-ops**: it clones the new sha, prepares metadata, then skips the
  install. `pip show` reports success either way and can't tell you the sha. Do:

  ```
  venv/bin/pip install --no-deps --force-reinstall \
    "wom.py @ git+https://github.com/DropTracker-io/wom.py@<sha>"
  ```

  and verify the sha that actually landed:

  ```
  cat venv/lib/python3.11/site-packages/wom_py-*.dist-info/direct_url.json
  ```

  Then restart the WOM-metric consumers — `droptracker-events`,
  `droptracker-player-updates`, `droptracker-webapi`, `droptracker-core` —
  since running processes hold the old module in memory.
- **Bigger jump:** upstream has a v3.x line (breaking client API). Migrating to
  it is a separate, larger task; the fork keeps us on a stable 1.0.0 base.
