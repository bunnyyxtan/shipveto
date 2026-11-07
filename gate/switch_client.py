"""The smallest Switch agent-protocol client this bot needs: one SSE stream
down, HTTP up, a heartbeat every 2 seconds (see Switch's agent-protocol doc)."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx_sse import aconnect_sse

log = logging.getLogger("switch")
ROOT = pathlib.Path(__file__).resolve().parent.parent
CREDENTIALS = ROOT / ".agent-credentials.json"


class SwitchAgent:
    def __init__(self, base_url: str) -> None:
        if not CREDENTIALS.exists():
            raise RuntimeError(f"{CREDENTIALS.name} is missing: register the agent first (python -m gate.setup)")
        creds = json.loads(CREDENTIALS.read_text())
        self.agent_id: str = creds["id"]
        self.connection_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"release-gate/{self.agent_id}"))
        self.cursor: int | None = None
        self.http = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {creds['api_key']}"}, timeout=httpx.Timeout(30, read=None)
        )

    async def op(self, operation: str, /, *, bound: bool = False, **args: Any) -> Any:
        """Call an operation; `bound` sends the open connection's id, which
        operations acting on the connection's current room need."""
        headers = {"X-Switch-Connection-Id": self.connection_id} if bound else {}
        r = await self.http.post(f"/agents/{self.agent_id}/ops/{operation}", json=args, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"Switch refused {operation}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()["result"]

    async def post(self, room_id: str, content: str) -> None:
        r = await self.http.post(f"/agents/{self.agent_id}/message", json={"room_id": room_id, "content": content})
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(f"Switch refused the message: HTTP {r.status_code} {r.text[:300]}")

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(2)
            body: dict[str, Any] = {"connection_id": self.connection_id}
            if self.cursor is not None:
                body["cursor"] = self.cursor
            r = await self.http.post(f"/agents/{self.agent_id}/connection/beat", json=body)
            if r.status_code in (404, 409):
                log.warning("heartbeat %s: stream detached, it will be reopened", r.status_code)
                return

    async def events(self, rooms: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Addressed events from `rooms`, forever, reopening the stream on loss."""
        while True:
            params = {
                "connection_id": self.connection_id,
                "scope": "single" if len(rooms) == 1 else "all",
                "filter": "addressed",
                "start_from": "head" if self.cursor is None else str(self.cursor),
                "rooms": ",".join(rooms),
                "client": "shipveto",
                "client_version": "0.1.0",
            }
            beat: asyncio.Task[None] | None = None
            try:
                async with aconnect_sse(self.http, "GET", f"/agents/{self.agent_id}/events", params=params) as source:
                    source.response.raise_for_status()
                    async for sse in source.aiter_sse():
                        data = json.loads(sse.data) if sse.data else {}
                        if sse.event == "connection_state":
                            log.info("connected to Switch, rooms=%s", data.get("rooms"))
                            beat = beat or asyncio.create_task(self._beat())
                            continue
                        if sse.event == "evicted":
                            log.warning("evicted: %s", data.get("reason"))
                            break
                        if "sequence" in data:
                            self.cursor = data["sequence"]
                        if sse.event in ("message", "gap"):
                            yield {"event": sse.event, **data}
            except httpx.HTTPError as exc:
                log.warning("stream lost (%s); reopening in 2s", exc)
            finally:
                if beat:
                    beat.cancel()
            await asyncio.sleep(2)
