"""Entrypoint for the DropTracker Web API v1 (separate process, port :31325).

Run (from the repo root, with the project venv):
    venv/bin/python -m web_api.app
or via Hypercorn directly (multi-worker):
    venv/bin/hypercorn --bind 127.0.0.1:31325 "web_api:create_app()"
"""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

load_dotenv()

from utils.sentry import init_sentry  # noqa: E402

init_sentry("droptracker-webapi")

from web_api import create_app  # noqa: E402


async def _main():
    app = create_app()
    port = int(os.environ.get("WEB_API_PORT", 31325))
    host = os.environ.get("WEB_API_HOST", "127.0.0.1")

    from hypercorn.config import Config
    from hypercorn.asyncio import serve

    config = Config()
    config.bind = [f"{host}:{port}"]
    config.graceful_timeout = 10
    config.keep_alive_timeout = 10

    print(f"Starting DropTracker Web API v1 on {host}:{port} ...")
    await serve(app, config)


if __name__ == "__main__":
    asyncio.run(_main())
