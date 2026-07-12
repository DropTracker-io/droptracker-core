# Deploying

To deploy the backend, run **`deploy/deploy.sh`** from the repo root. One
command does the whole ritual: `git pull --ff-only` → `pip install` →
**alembic migration guard** (auto-runs `alembic upgrade head` if the DB is
behind the code — deploying with an unapplied migration has caused live 1054
errors before) → unit tests → `sudo systemctl restart` of the scoped units →
health checks (`:31323/ping`, `:31325/api/v1/health`, `systemctl is-active`)
→ PASS/FAIL summary. Scope restarts with `--api`, `--webapi`, `--bots`,
`--workers`, `--boards` (default `--all`); skip steps with `--no-pull` /
`--skip-tests`; rehearse with `--dry-run`. The script assumes the invoking
user can `sudo systemctl`. The web frontend has its own script:
`scripts/deploy.sh` in the web repo (`/store/droptracker/web`).

The systemd unit files themselves are vendored in [`systemd/`](systemd/README.md)
— see that README for the unit ↔ entry-point map and install instructions.
