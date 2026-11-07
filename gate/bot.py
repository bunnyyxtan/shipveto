"""The release-gate agent: lives in a Switch room, takes instructions from the
people in it, and runs, reports, corrects and approves support-agent versions.

    SWITCH_URL=http://<host>:8000 python -m gate.bot   (OPENAI_API_KEY in .env)

Commands (mention @release-gate):
    test v2 [against v1] [fresh]
                             run every saved scenario on both, report regressions;
                             reuses an identical earlier live run unless "fresh"
    fix v2: <rule>           make v2.1 = v2 plus your rule, show the diff, retest it
    approve v2.1 [note]      only if its latest live run passed every scenario
    verify                   re-check the latest approval against the files
    help
"""

from __future__ import annotations

import asyncio
import datetime as dt
import difflib
import json
import logging
import os
import re
from typing import Any

from gate import approvals, evaluate
from gate.setup import ROOM_FILE
from gate.switch_client import SwitchAgent

log = logging.getLogger("release-gate")
BASELINE = "v1"
TRIALS = int(os.environ.get("GATE_TRIALS", "2"))
HELP = __doc__.split("Commands (mention @release-gate):")[1]


def next_version(version: str) -> str:
    parts = version[1:].split(".")
    if len(parts) == 1:
        return f"{version}.1"
    return "v" + ".".join(parts[:-1] + [str(int(parts[-1]) + 1)])


def reusable(record: dict[str, Any] | None, version: str) -> bool:
    return bool(
        record
        and record["mode"] == "live"
        and record["model"] == evaluate.MODEL
        and record["trials"] == TRIALS
        and record["prompt_sha256"] == evaluate.sha256_file(evaluate.prompt_path(version))
        and record["scenarios_sha256"] == evaluate.sha256_file(evaluate.SCENARIOS)
    )


class ReleaseGate:
    def __init__(self, agent: SwitchAgent, room_id: str) -> None:
        self.agent = agent
        self.room_id = room_id
        self.busy = asyncio.Lock()

    async def say(self, text: str) -> None:
        await self.agent.post(self.room_id, text)

    async def handle(self, event: dict[str, Any]) -> None:
        payload = event["payload"]
        who = payload.get("sender_name") or payload.get("sender", "someone")
        text = re.sub(r"@[\w.-]+", "", payload.get("body", "")).strip()
        log.info("%s: %s", who, text)
        try:
            if m := re.match(r"(?i)test\s+(v[\d.]+)(?:\s+against\s+(v[\d.]+))?(\s+fresh)?\s*$", text):
                await self.test(m.group(1), m.group(2) or BASELINE, fresh=bool(m.group(3)))
            elif m := re.match(r"(?is)fix\s+(v[\d.]+)\s*:\s*(.+)", text):
                await self.fix(m.group(1), m.group(2).strip(), who)
            elif m := re.match(r"(?is)approve\s+(v[\d.]+)\s*(.*)", text):
                await self.approve(m.group(1), who, m.group(2).strip())
            elif re.match(r"(?i)verify\b", text):
                await self.verify()
            else:
                await self.say(f"Commands:\n```{HELP}```")
        except Exception as exc:  # one bad command must not stop the bot
            log.exception("command failed")
            await self.say(f":warning: That failed: `{exc}`")

    async def run_version(self, version: str, fresh: bool) -> tuple[dict[str, Any], bool]:
        cached = evaluate.latest_run(version)
        if not fresh and reusable(cached, version):
            assert cached is not None
            return cached, True
        return await asyncio.to_thread(evaluate.run_live, version, TRIALS), False

    async def test(self, candidate: str, baseline: str, fresh: bool = False) -> None:
        evaluate.prompt_path(candidate)
        n = len(evaluate.load_scenarios())
        async with self.busy:
            await self.say(
                f"Testing **{candidate}** against **{baseline}**: {n} saved customer scenarios x {TRIALS} trials each, "
                f"live on `{evaluate.MODEL}`. Tools are sandboxed: nothing is refunded for real."
            )
            base, base_cached = await self.run_version(baseline, fresh)
            cand, cand_cached = await self.run_version(candidate, fresh)
        notes = [
            f"\n_{record['version']} result reused from live run `{record['run_id']}` (identical prompt, scenarios, model "
            f"and trials; say `fresh` to re-run)._"
            for record, cached in ((base, base_cached), (cand, cand_cached))
            if cached
        ]
        await self.say(evaluate.compare_markdown(base, cand) + "".join(notes) + self.evidence(cand))

    def evidence(self, record: dict[str, Any]) -> str:
        failed = next((r for r in record["results"] if not r["passed"]), None)
        if failed is None:
            return ""
        calls = "\n".join(f"{c['name']}({json.dumps(c['input'])})" for c in failed["tool_calls"])
        return (
            f"\n\nEvidence from `{failed['scenario_id']}` trial {failed['trial']}, the tool calls the support agent made:\n"
            f"```\n{calls or '(none)'}\n```\nIts reply: > {failed['reply'][:400]}"
        )

    async def fix(self, version: str, rule: str, who: str) -> None:
        source = evaluate.prompt_path(version)
        new = next_version(version)
        while (evaluate.PROMPTS / f"support-{new}.md").exists():
            new = next_version(new)
        target = evaluate.PROMPTS / f"support-{new}.md"
        old_text = source.read_text(encoding="utf-8")
        day = dt.date.today().isoformat()
        new_text = old_text.rstrip("\n") + f"\n\nRelease-gate correction requested by {who} on {day}:\n- {rule}\n"
        target.write_text(new_text, encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                old_text.splitlines(True), new_text.splitlines(True), source.name, target.name
            )
        )
        await self.say(f"Created **{new}** from {version} with your rule.\n```diff\n{diff}```")
        await self.test(new, BASELINE)

    async def approve(self, version: str, who: str, note: str) -> None:
        run = evaluate.latest_run(version)
        if run is None:
            await self.say(f":no_entry: {version} has never been tested. Ask me to `test {version}` first.")
            return
        try:
            path, artifact = approvals.approve(run, who, self.room_id, note)
        except approvals.ApprovalRefused as exc:
            await self.say(f":no_entry: **Release blocked.** {exc}.")
            return
        await self.agent.op(
            "create_room_document",
            bound=True,
            name=f"release-approval-{version}",
            description=f"Approval of support agent {version}",
            instructions="The signed-off release record. Do not edit; re-run `verify` to check it.",
            content=json.dumps(artifact, indent=2),
        )
        e = artifact["evaluation"]
        await self.say(
            f":white_check_mark: **{version} approved by {who}.**\n"
            f"- prompt `sha256:{artifact['prompt_sha256'][:16]}…`\n"
            f"- evaluation `{e['run_id']}`: {e['scenarios_passed']}/{e['scenarios_total']} scenarios, {e['trials']} trials, `{e['model']}`\n"
            f"- record `{path.name}`, digest `{artifact['digest_sha256'][:16]}…`, also saved to this room's documents\n"
            f"Anyone can check it: `python -m gate.approvals approvals/{path.name}`"
        )

    async def verify(self) -> None:
        files = sorted(approvals.APPROVALS.glob("*.json")) if approvals.APPROVALS.exists() else []
        if not files:
            await self.say("No approvals yet.")
            return
        problems = approvals.verify(files[-1])
        if problems:
            await self.say(f":x: `{files[-1].name}` is INVALID: " + "; ".join(problems))
        else:
            await self.say(f":white_check_mark: `{files[-1].name}` is VALID: every hash matches the files on disk.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required: ShipVeto only approves on live model runs.")
    room_id = json.loads(ROOM_FILE.read_text())["id"]
    agent = SwitchAgent(os.environ.get("SWITCH_URL", "http://127.0.0.1:8000"))
    gate = ReleaseGate(agent, room_id)
    async for event in agent.events([room_id]):
        if event["event"] == "gap":
            log.warning("gap in the event stream: %s", event)
            continue
        if not event.get("payload", {}).get("addressed"):
            continue
        if event["payload"].get("sender") == agent.agent_id:
            continue
        asyncio.create_task(gate.handle(event))


if __name__ == "__main__":
    asyncio.run(main())
