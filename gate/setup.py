"""One-time setup: register the release-gate agent with Switch and create its
room, bridged to a new Mattermost channel with the named people in it.

    SWITCH_URL=http://<host>:8000 SWITCH_ENV=../switch/.env python -m gate.setup --users user
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib

import httpx

from gate.switch_client import CREDENTIALS, ROOT, SwitchAgent

ROOM_FILE = ROOT / ".gate-room.json"
ROOM_NAME = "support-release-gate"
ROOM_DESCRIPTION = "Test support-agent versions against saved customer scenarios, fix regressions, approve releases."


def switch_env(key: str) -> str:
    env = pathlib.Path(os.environ.get("SWITCH_ENV", ROOT.parent / "switch" / ".env"))
    for line in env.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{key} is not in {env}")


def registration_token() -> str:
    return switch_env("AGENT_REGISTRATION_TOKEN")


def allow_people_in_room(base_url: str, room_id: str) -> None:
    """Let the people in the gate room address the agent there, and nowhere
    else; no other agent may. Set as the server's admin through the Gateway."""
    agent_id = json.loads(CREDENTIALS.read_text())["id"]
    with httpx.Client(base_url=f"{base_url}/gateway", timeout=20) as gw:
        login = gw.post(
            "/auth/login",
            json={"email": switch_env("GATEWAY_ADMIN_EMAIL"), "password": switch_env("GATEWAY_ADMIN_PASSWORD")},
        )
        login.raise_for_status()
        r = gw.put(
            f"/agents/{agent_id}/addressing-policy",
            json={"policy": {"rules": [{"rooms": [room_id], "users": "*", "agents": []}]}},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Could not set the addressing policy: HTTP {r.status_code} {r.text[:300]}")
    print("addressing: people in the gate room only")


def register(base_url: str) -> None:
    if CREDENTIALS.exists():
        return
    r = httpx.post(
        f"{base_url}/agents",
        headers={"Authorization": f"Bearer {registration_token()}"},
        json={
            "name": "release-gate",
            "display_name": "ShipVeto",
            "description": "Tests a support-agent candidate against saved customer scenarios before release",
            "connector_type": "custom",
            "integration_profile": {
                "connection_model": "always_on",
                "message_exchange": True,
                "pre_invocation_mediation": [],
                "post_invocation_mediation": [],
                "event_reporting": [],
            },
        },
        timeout=20,
    )
    r.raise_for_status()
    CREDENTIALS.write_text(json.dumps(r.json()))
    print("registered agent release-gate")


async def create_room(base_url: str, users: list[str]) -> None:
    agent = SwitchAgent(base_url)
    bridges = await agent.op("list_bridges")
    mattermost = next((b for b in bridges if b["type"] == "mattermost" and b["status"] == "active"), None)
    if mattermost is None:
        raise RuntimeError(f"No active Mattermost bridge in Switch: {bridges}")
    existing = [r for r in await agent.op("list_rooms") if r["name"] == ROOM_NAME and not r["archived"]]
    if existing:
        room = {"id": existing[0]["room_id"], "name": ROOM_NAME}
    else:
        room = await agent.op(
            "create_room",
            name=ROOM_NAME,
            description=ROOM_DESCRIPTION,
            agent_names=["release-gate"],
            user_names=users,
            bridge_id=mattermost["id"],
        )
    ROOM_FILE.write_text(json.dumps(room, indent=2))
    print("room:", json.dumps(room)[:400])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", nargs="+", required=True, help="Mattermost usernames to add to the room")
    args = parser.parse_args()
    base_url = os.environ.get("SWITCH_URL", "http://127.0.0.1:8000")
    register(base_url)
    asyncio.run(create_room(base_url, args.users))
    allow_people_in_room(base_url, json.loads(ROOM_FILE.read_text())["id"])


if __name__ == "__main__":
    main()
