"""Post as the local Mattermost test user and print the channel's latest posts.
Used for smoke tests; the demo itself is typed in the Mattermost UI.

    python scripts/mm_say.py "@release-gate help"      # post, wait, show replies
    python scripts/mm_say.py --read                    # only show recent posts
"""

import argparse
import os
import pathlib
import time

import httpx

ENV = pathlib.Path(os.environ.get("SWITCH_ENV", pathlib.Path(__file__).resolve().parents[2] / "switch" / ".env"))
MM = os.environ.get("MATTERMOST_URL", "http://127.0.0.1:8065")


def env(key: str) -> str:
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not in {ENV}")


parser = argparse.ArgumentParser()
parser.add_argument("text", nargs="?")
parser.add_argument("--read", action="store_true")
parser.add_argument("--wait", type=int, default=8)
parser.add_argument("--count", type=int, default=6)
args = parser.parse_args()

with httpx.Client(base_url=MM, timeout=20) as mm:
    login = mm.post("/api/v4/users/login", json={"login_id": env("MATTERMOST_USER"), "password": env("MATTERMOST_USER_PASSWORD")})
    login.raise_for_status()
    mm.headers["Authorization"] = f"Bearer {login.headers['Token']}"
    me = login.json()["id"]
    team = mm.get(f"/api/v4/teams/name/{env('MATTERMOST_TEAM_NAME')}").json()["id"]
    channels = mm.get(f"/api/v4/users/{me}/teams/{team}/channels").json()
    channel = next((c for c in channels if "release-gate" in c["name"] or "release-gate" in c["display_name"].lower()), None)
    if channel is None:
        raise SystemExit("no release-gate channel: " + ", ".join(c["display_name"] for c in channels))
    if args.text:
        mm.post("/api/v4/posts", json={"channel_id": channel["id"], "message": args.text}).raise_for_status()
        time.sleep(args.wait)
    posts = mm.get(f"/api/v4/channels/{channel['id']}/posts", params={"per_page": args.count}).json()
    users: dict[str, str] = {}
    for pid in reversed(posts["order"]):
        p = posts["posts"][pid]
        uid = p["user_id"]
        users.setdefault(uid, mm.get(f"/api/v4/users/{uid}").json().get("username", uid))
        print(f"--- {users[uid]} {p.get('props', {}).get('override_username', '')}\n{p['message'][:3000]}")
