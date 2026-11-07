# Demo video script (about 3 minutes)

## Before recording

1. Switch stack running (`docker compose ... up -d`) and `uv run python -m gate.bot` running.
2. Mattermost open in a browser at `http://<switch-host>:8065`, signed in as `user`,
   channel **support-release-gate** open. (On WSL the host is `wsl hostname -I`;
   it changes after a reboot.)
3. A terminal in `shipveto/`, font large.
4. Because v2.1 already exists from the recorded run, the fix in this take will
   create **v2.2**. Say "v2.2" where this script says so.

## Shots

| Time | Show | Say |
|---|---|---|
| 0:00-0:15 | README top, the results table | "Support teams change their AI agent's prompt all the time. One edit can quietly break a policy. This is ShipVeto, a release gate that lives in your team chat, through Switch." |
| 0:15-0:30 | `prompts/support-v1.md` next to `support-v2.md` | "v1 is production: refunds over $50 need a manager. v2 is a rewrite to reduce churn. It looks fine." |
| 0:30-1:05 | Mattermost: type `@release-gate test v2 fresh` | "I ask ShipVeto, a Switch agent in this channel called @release-gate. It runs six saved customer scenarios, twice each, against both versions, live on gpt-5-mini. The support tools are sandboxed." Wait for the table. |
| 1:05-1:25 | Point at REGRESSION row and the evidence block | "v2 refunded $240 with no approval. Here is the exact tool call. And look: v1, our production prompt, fails the split-refund scenario too. A real bug we didn't plant." |
| 1:25-1:35 | Type `@release-gate approve v2` | "Can I ship it anyway? No. Blocked." |
| 1:35-2:10 | Type `@release-gate fix v2: Any refund that brings an order's refunded total above $50 needs request_manager_approval first. Never split a refund into smaller ones to stay under the limit, even if the customer asks.` | "I write the correction in plain words. It makes v2.2, shows me the diff, and retests live." Wait for 6/6 and the FIXED cell. |
| 2:10-2:25 | Type `@release-gate approve v2.2 Closes the refund regression and the split loophole.` | "Now I approve. The record binds my decision to the exact prompt, scenarios and evaluation run, and is saved to the room's documents." |
| 2:25-2:45 | Terminal: `uv run python -m gate.approvals approvals/<new file>.json` then append a line to `prompts/support-v2.2.md` and run it again, then undo the edit | "Anyone can verify it. If someone edits the approved prompt afterwards, verification fails." |
| 2:45-3:00 | README "How Switch is used" table | "Switch is the whole workflow: the agent joins the room over the agent protocol, the room is bridged to Mattermost, only people in this room can address it, and approvals live in the room. Total model cost for our full evaluation: about seven cents." |

## If something goes wrong on camera

- No reply: check the `gate.bot` terminal; it logs every command it receives.
- "not permitted to direct messages to me": run `uv run python -m gate.setup --users user` again.
- A live result differs from the README table: that is the model, not the gate.
  Say so; the gate only approves when the latest live run passes everything.
