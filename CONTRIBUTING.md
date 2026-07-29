# Contributing to DropTracker

Thanks for your interest in helping build DropTracker! This guide covers the practical side of getting changes merged. For a map of the codebase, start with the [README](README.md), then [CLAUDE.md](CLAUDE.md) (a dense orientation doc kept current for both humans and AI coding agents) and [docs/](docs/).

## Branches & CI

- **`new-api` is the active development branch.** Branch from it and target it with your PRs.
- CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs `pytest tests/unit/` on pushes and PRs to `new-api`. Keep it green.

## Development setup

Follow the [Getting started](README.md#getting-started-development) section of the README. The short version:

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # fill in DB, Redis, and a dev Discord bot token
```

You only need to run the process you're working on — the system is deliberately multi-process, and each entry point boots independently (see the [runtime process table](README.md#runtime-processes)).

**A real MySQL database is the main setup hurdle.** Migration files (`alembic/versions/`) are not committed to the repo, so a fresh clone can't build the schema from scratch — ask a maintainer (Discord is fastest) for a schema dump to import. After that, Alembic works normally for your own changes.

You'll also want a **dev Discord bot**: create an application at the Discord developer portal, put its token in `DEV_TOKEN`, and set `STATE=dev` so the bots use it instead of the production token.

## Tests

```bash
pytest tests/unit -q          # fast, fully mocked — what CI runs
pytest tests/integration -q   # needs live MySQL + Redis
```

`tests/conftest.py` stubs the environment and heavy modules, so unit tests run without any services or secrets. Please add unit coverage for new logic in `data/submissions/`, `services/`, or `web_api/routes/` — those areas already have test patterns you can copy.

## Database changes

1. Add/modify the model in `db/models/` (and export it from `db/models/__init__.py`).
2. Generate a migration: `alembic revision --autogenerate -m "describe the change"`.
3. Review the generated file carefully — autogenerate is noisy with this schema.
4. Include the migration file contents in your PR description (since `alembic/versions/` is gitignored, reviewers can't see it in the diff).
5. Check `alembic heads` — it must print exactly one. Because the version files aren't shared, it's easy to author a migration off a head that has since moved and split the graph in two; `alembic upgrade head` then refuses to run at all. Merge the split immediately with `alembic merge -m "why these lines diverged" heads` (no DDL — it just rejoins the graph). `tests/unit/test_alembic_single_head.py` guards this locally.

Two things to keep in mind: the ORM spans **two MySQL schemas** (`data` and `xenforo`), and several submission tables have `seasonal_*` mirrors that usually need the same change.

## Code conventions

- **Async everywhere.** The whole stack is asyncio (Quart, discord-py-interactions, aiomysql). Don't introduce blocking calls in request/event handlers; long work belongs in a background worker or queue.
- **Processors never talk to Discord.** Submission processors write `NotificationQueue` rows; the core bot sends the messages. Keep that separation.
- **Identity goes through Wise Old Man.** Don't create `Player` rows or trust RSNs without the WOM checks in `data/submissions/common.py`.
- **Watch the intake hot path.** `api/routes/webhook.py` and the processors run at high volume — avoid adding per-submission queries or external calls there without discussing first.
- **Config over code** for per-group behavior: group settings live in the `group_configurations` key-value table (see `web_api/config_registry.py` for the schema).
- Match the style of the file you're editing; there's no enforced formatter or linter yet.

## Secrets & safety

- Never commit `.env`, tokens, or database dumps. `.gitignore` covers the usual suspects — check `git status` before committing.
- If you add a new environment variable, add it to `.env.example` with a comment.
- This is a production service with real user data. Changes to intake, points, subscriptions, or anything money-adjacent get extra scrutiny.

## Good places to start

- **Bug fixes / small features** in slash commands (`commands/`) or web API routes (`web_api/routes/`) — well-isolated, easy to test.
- **Unit tests** for existing processors and services — coverage is still thin.
- **CI improvements** — linting and integration-test jobs don't exist yet.
- Check the "Known Issues / Tech Debt" section of [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md).

Avoid the legacy code listed in the [README](README.md#legacy-code-what-to-ignore) — PRs building on those paths won't be merged.

## Questions?

Open a GitHub issue or ask in the [DropTracker Discord](https://www.droptracker.io/) — that's where the maintainers are most responsive.
