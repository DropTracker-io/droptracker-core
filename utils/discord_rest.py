"""A gateway-less Discord sender.

The bots talk to Discord over a gateway connection, which is right for anything
that needs to *listen*. A monthly recap run only needs to *post*: open a DM
channel, send a message. Standing up a second gateway client for that would
burn an identify against the same bot's session limit and take a minute to
connect, for two REST calls per recipient.

So this is deliberately small — two endpoints and honest error mapping — and it
raises the same exception types the interactions library does, so
:class:`utils.discord_write.DiscordWriter` can pace and retry it without knowing
which transport produced the error. That is the only reason those imports are
here; nothing else in this module depends on the library.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

import aiohttp
from interactions.client.errors import (
    BadRequest,
    Forbidden,
    HTTPException,
    NotFound,
    RateLimited,
)

API = "https://discord.com/api/v10"


class DiscordRest:
    """Minimal REST poster. Use as an async context manager."""

    def __init__(self, token: str, *, user_agent: str = "DropTracker-recaps/1.0"):
        if not token:
            raise ValueError("DiscordRest needs a bot token")
        self._token = token
        self._ua = user_agent
        self._session: Optional[aiohttp.ClientSession] = None
        # A DM channel id never changes for a given user, and opening one is a
        # POST against a rate-limited bucket — worth remembering for a run that
        # may message the same person twice.
        self._dm_channels: Dict[str, str] = {}

    async def __aenter__(self) -> "DiscordRest":
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bot {self._token}",
                "User-Agent": self._ua,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        if self._session is None:
            raise RuntimeError("DiscordRest used outside its context manager")
        async with self._session.request(method, f"{API}{path}", json=payload) as resp:
            body: Any = None
            if resp.content_type == "application/json":
                body = await resp.json()
            else:
                body = await resp.text()

            if resp.status in (200, 201):
                return body
            if resp.status == 204:
                return None
            # These constructors take the aiohttp response itself (they read
            # .status off it) — handing them a bare string makes __init__ blow
            # up with AttributeError, which turns every 403/429 into an
            # untyped failure downstream.
            if resp.status == 429:
                retry_after = 1.0
                if isinstance(body, dict):
                    retry_after = float(body.get("retry_after") or 1.0)
                exc = RateLimited(resp, text=f"429 on {path}")
                # DiscordWriter reads this attribute to size its backoff; the
                # library's own exception carries it under the same name.
                exc.retry_after = retry_after
                raise exc
            if resp.status == 403:
                # The one failure that is a fact about the recipient rather than
                # about us: DMs closed, or no shared server.
                raise Forbidden(resp, text=f"403 on {path}: {body}")
            if resp.status == 404:
                raise NotFound(resp, text=f"404 on {path}: {body}")
            if resp.status == 400:
                raise BadRequest(resp, text=f"400 on {path}: {body}")
            raise HTTPException(resp, text=f"{resp.status} on {path}: {body}")

    async def open_dm(self, discord_user_id: str) -> str:
        """The DM channel with this user, opening one if needed."""
        key = str(discord_user_id)
        cached = self._dm_channels.get(key)
        if cached:
            return cached
        data = await self._request(
            "POST", "/users/@me/channels", {"recipient_id": key}
        )
        channel_id = str((data or {}).get("id") or "")
        if not channel_id:
            # No live response object to hand over here (the request itself
            # succeeded), so fake the attributes the constructor reads.
            raise HTTPException(
                SimpleNamespace(status=200, reason="OK"),
                text=f"no channel id returned for user {key}",
            )
        self._dm_channels[key] = channel_id
        return channel_id

    async def post_message(self, channel_id: str, message: dict) -> Optional[str]:
        """Post one message, returning its id.

        ``message`` is the raw Discord payload (``content``/``embeds``/
        ``components``); empty keys are dropped so a content-less embed post
        doesn't send ``"content": null``.
        """
        payload = {k: v for k, v in (message or {}).items() if v not in (None, [], "")}
        data = await self._request("POST", f"/channels/{channel_id}/messages", payload)
        return str((data or {}).get("id") or "") or None

    async def edit_message(self, channel_id: str, message_id: str, message: dict) -> None:
        payload = {k: v for k, v in (message or {}).items() if v not in (None, [], "")}
        await self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}", payload
        )

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")

    async def send_dm(self, discord_user_id: str, message: dict) -> Optional[str]:
        channel_id = await self.open_dm(discord_user_id)
        return await self.post_message(channel_id, message)
