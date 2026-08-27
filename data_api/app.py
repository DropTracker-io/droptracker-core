"""Entry point: python -m data_api.app (dev) — production runs hypercorn:

    venv/bin/hypercorn --bind 127.0.0.1:31326 "data_api:create_app()"
"""
import asyncio
import os

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    from data_api import create_app

    port = int(os.getenv("DATA_API_PORT", "31326"))
    config = Config()
    config.bind = [f"127.0.0.1:{port}"]
    asyncio.run(serve(create_app(), config))


if __name__ == "__main__":
    main()
