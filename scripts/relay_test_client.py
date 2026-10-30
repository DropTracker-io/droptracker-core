"""A stand-in "customer bot" for the Discord delivery relay (web153a).

Does exactly what a group's own bot must do to deliver DropTracker's
messages, so the relay can be tested end to end on the dev instance with a
throwaway Discord application — and serves as the reference for real
integrations (the TrueGritty py-cord cog follows the same steps):

1. ``GET /v2/relay/integration`` — guilds, interaction defer rules.
2. Report each owned guild's channels (``PUT /v2/relay/channels``) so routing
   works even if DropTracker's bot is not in the server.
3. Long-poll ``GET /v2/relay/messages?wait=20``; perform each create / edit /
   delete against Discord with THIS bot's token (multipart when the message
   carries files, fetched from ``/v2/relay/files/...``); ack the outcome.
4. On a component click whose custom_id matches a rule: defer it at once
   (``ephemeral`` → type 5 + flags 64, ``update`` → type 6), then forward the
   raw interaction to ``POST /v2/relay/interactions``. DropTracker answers it
   through the interaction token.

Usage (dev):
    venv/bin/python -m scripts.relay_test_client \\
        --api http://127.0.0.1:31326/v2 --key-file ~/relay-test/key \\
        --token-file ~/relay-test/bot_token

Secrets are read from files (never argv/env), so they don't land in shell
history or process listings.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp

DISCORD = "https://discord.com/api/v10"
UA = "DropTracker-RelayTestClient (https://www.droptracker.io, 1.0)"
log = logging.getLogger("relay_test_client")


class RelayClient:
    def __init__(self, api: str, key: str, bot_token: str):
        self.api = api.rstrip("/")
        self.key = key
        self.bot_token = bot_token
        self.rules: list = []
        self.http: aiohttp.ClientSession | None = None

    # -- DropTracker side ---------------------------------------------------
    def _dt_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "User-Agent": UA}

    async def dt(self, method: str, path: str, **kw):
        async with self.http.request(method, f"{self.api}{path}",
                                     headers=self._dt_headers(), **kw) as resp:
            if resp.content_type == "application/json":
                return resp.status, await resp.json()
            return resp.status, await resp.read()

    # -- Discord side -------------------------------------------------------
    def _bot_headers(self) -> dict:
        return {"Authorization": f"Bot {self.bot_token}", "User-Agent": UA}

    async def discord(self, method: str, path: str, body=None, files=None):
        """``(status, json)``. Multipart when ``files`` is a list of
        ``(index, filename, bytes)``."""
        url = f"{DISCORD}{path}"
        if files:
            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps(body or {}),
                           content_type="application/json")
            for index, name, data in files:
                form.add_field(f"files[{index}]", data, filename=name)
            kw = {"data": form}
        elif body is not None:
            kw = {"json": body}
        else:
            kw = {}
        async with self.http.request(method, url, headers=self._bot_headers(), **kw) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = None
            return resp.status, data

    # -- steps --------------------------------------------------------------
    async def describe(self) -> dict:
        status, info = await self.dt("GET", "/relay/integration")
        if status != 200:
            raise SystemExit(f"integration lookup failed: {status} {info}")
        self.rules = info["interaction_rules"]
        log.info("integration %s for group %s, guilds %s, status %s",
                 info["integration_id"], info["group_id"], info["guild_ids"], info["status"])
        return info

    async def report_channels(self, guild_ids) -> None:
        for gid in guild_ids:
            status, chans = await self.discord("GET", f"/guilds/{gid}/channels")
            if status != 200:
                log.warning("can't list channels in %s: %s %s", gid, status, chans)
                continue
            ids = [c["id"] for c in chans]
            status, out = await self.dt("PUT", "/relay/channels",
                                        json={"guild_id": gid, "channel_ids": ids})
            log.info("reported %s channels for %s: %s %s", len(ids), gid, status, out)

    async def deliver(self, msg: dict) -> None:
        files = []
        for f in msg.get("files") or []:
            status, data = await self.dt("GET", f["url"].removeprefix("/v2"))
            if status != 200:
                await self.ack(msg["id"], ok=False, http_status=0,
                               error=f"file fetch {status}")
                return
            files.append((f["index"], f["filename"], data))
        cid = msg["channel_id"]
        if msg["op"] == "create":
            status, data = await self.discord("POST", f"/channels/{cid}/messages",
                                              msg["body"], files)
        elif msg["op"] == "edit":
            status, data = await self.discord(
                "PATCH", f"/channels/{cid}/messages/{msg['message_id']}", msg["body"], files)
        else:
            status, data = await self.discord(
                "DELETE", f"/channels/{cid}/messages/{msg['message_id']}")
        if status < 300:
            await self.ack(msg["id"], ok=True,
                           discord_message_id=(data or {}).get("id"))
        else:
            await self.ack(msg["id"], ok=False, http_status=status,
                           code=(data or {}).get("code"), error=(data or {}).get("message"))

    async def ack(self, mid, **body) -> None:
        status, out = await self.dt("POST", f"/relay/messages/{mid}/ack", json=body)
        log.info("ack %s %s -> %s %s", mid, body, status, out)

    async def poll_forever(self) -> None:
        while True:
            began = time.monotonic()
            try:
                status, out = await self.dt("GET", "/relay/messages?wait=20&limit=20")
            except Exception as exc:
                log.warning("poll failed: %s", exc)
                await asyncio.sleep(5)
                continue
            if status != 200:
                log.warning("poll: %s %s", status, out)
                await asyncio.sleep(10)
                continue
            if not out.get("messages") and time.monotonic() - began < 2:
                # An instant empty answer (old server, or paused without a
                # wait) must never turn the loop into a hot spin.
                await asyncio.sleep(5)
            for msg in out.get("messages") or []:
                log.info("deliver %s %s -> channel %s", msg["id"], msg["op"], msg["channel_id"])
                try:
                    await self.deliver(msg)
                except Exception:
                    log.exception("deliver %s failed", msg["id"])

    def defer_mode(self, custom_id: str):
        for rule in self.rules:
            if custom_id.startswith(rule["prefix"]):
                return rule["defer"]
        return None

    async def on_interaction(self, payload: dict) -> None:
        if payload.get("type") != 3:
            return
        custom_id = (payload.get("data") or {}).get("custom_id") or ""
        mode = self.defer_mode(custom_id)
        if mode is None:
            return  # not DropTracker's — the bot's own handlers deal with it
        callback = ({"type": 5, "data": {"flags": 64}} if mode == "ephemeral"
                    else {"type": 6})
        async with self.http.post(
                f"{DISCORD}/interactions/{payload['id']}/{payload['token']}/callback",
                json=callback, headers={"User-Agent": UA}) as resp:
            log.info("deferred %s as %s -> %s", custom_id, mode, resp.status)
        status, out = await self.dt("POST", "/relay/interactions", json=payload)
        log.info("forwarded %s -> %s %s", custom_id, status, out)
        if status != 202:
            async with self.http.post(
                    f"{DISCORD}/webhooks/{payload['application_id']}/{payload['token']}",
                    json={"content": "That button isn't available right now.", "flags": 64},
                    headers={"User-Agent": UA}):
                pass


async def main_async(args) -> None:
    import interactions
    from interactions.api.events import RawGatewayEvent

    client = RelayClient(args.api, Path(args.key_file).expanduser().read_text().strip(),
                         Path(args.token_file).expanduser().read_text().strip())
    bot = interactions.Client(token=client.bot_token, intents=interactions.Intents.GUILDS)

    @interactions.listen("raw_interaction_create")
    async def _raw(event: RawGatewayEvent):
        try:
            await client.on_interaction(event.data or {})
        except Exception:
            log.exception("interaction relay failed")

    bot.add_listener(_raw)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as http:
        client.http = http
        info = await client.describe()
        await client.report_channels(info["guild_ids"])
        poller = asyncio.create_task(client.poll_forever())
        try:
            await bot.astart()
        finally:
            poller.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api", required=True, help="data API base, e.g. https://api.droptracker.io/v2")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
