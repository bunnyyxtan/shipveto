"""Release approvals: a JSON record binding a decision to exactly what was tested.

`verify` recomputes every hash from the files on disk, so an approval stops
verifying if the prompt, the scenarios, the evaluation run or the record
itself is edited after the decision.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Any

from gate.evaluate import RUNS, SCENARIOS, prompt_path, sha256_file

APPROVALS = pathlib.Path(__file__).resolve().parent.parent / "approvals"


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ApprovalRefused(Exception):
    pass


def approve(run: dict[str, Any], approver: str, room_id: str, note: str) -> tuple[pathlib.Path, dict[str, Any]]:
    if run["mode"] != "live":
        raise ApprovalRefused(f"run {run['run_id']} is a {run['mode']} run; only a live run can back a release")
    if not run["summary"]["all_passed"]:
        s = run["summary"]
        raise ApprovalRefused(f"{run['version']} passes {s['scenarios_passed']}/{s['scenarios_total']} scenarios")
    if sha256_file(prompt_path(run["version"])) != run["prompt_sha256"]:
        raise ApprovalRefused(f"prompts/support-{run['version']}.md changed after run {run['run_id']}; test it again")
    if sha256_file(SCENARIOS) != run["scenarios_sha256"]:
        raise ApprovalRefused(f"scenarios.yaml changed after run {run['run_id']}; test it again")
    run_file = RUNS / f"{run['run_id']}.json"
    body = {
        "decision": "approved",
        "version": run["version"],
        "prompt_file": f"prompts/support-{run['version']}.md",
        "prompt_sha256": run["prompt_sha256"],
        "scenarios_sha256": run["scenarios_sha256"],
        "evaluation": {
            "run_id": run["run_id"],
            "run_file_sha256": sha256_file(run_file),
            "model": run["model"],
            "trials": run["trials"],
            **run["summary"],
            "per_scenario": {r["scenario_id"]: r["passed"] for r in run["results"] if r["trial"] == 1},
        },
        "approved_by": approver,
        "switch_room_id": room_id,
        "note": note,
        "approved_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    artifact = {**body, "digest_sha256": _digest(body)}
    APPROVALS.mkdir(exist_ok=True)
    path = APPROVALS / f"{run['version']}-{body['approved_at'].replace(':', '')}.json"
    path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return path, artifact


def verify(path: pathlib.Path) -> list[str]:
    artifact = json.loads(path.read_text(encoding="utf-8"))
    body = {k: v for k, v in artifact.items() if k != "digest_sha256"}
    problems = []
    if _digest(body) != artifact.get("digest_sha256"):
        problems.append("the approval record was edited after it was written")
    if sha256_file(prompt_path(artifact["version"])) != artifact["prompt_sha256"]:
        problems.append(f"{artifact['prompt_file']} no longer matches the approved prompt")
    if sha256_file(SCENARIOS) != artifact["scenarios_sha256"]:
        problems.append("scenarios.yaml no longer matches the tested scenarios")
    run_file = RUNS / f"{artifact['evaluation']['run_id']}.json"
    if not run_file.exists():
        problems.append(f"evaluation run {run_file.name} is missing")
    elif sha256_file(run_file) != artifact["evaluation"]["run_file_sha256"]:
        problems.append(f"evaluation run {run_file.name} was edited")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a release approval against the files on disk")
    parser.add_argument("approval", type=pathlib.Path)
    problems = verify(parser.parse_args().approval)
    for p in problems:
        print(f"INVALID: {p}")
    if problems:
        sys.exit(1)
    print("VALID: prompt, scenarios, evaluation run and decision all match their recorded hashes")


if __name__ == "__main__":
    main()
