"""Run saved customer scenarios against one version of the support agent.

Two modes, always labelled in every record and report:
- live:   the support agent is a real OpenAI model call (Responses API with
          function calling) using the version's prompt; its tools are
          sandboxed stubs that only record what it tried.
- replay: re-judges a previously recorded live run's traces. No model call.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
for _line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
    _key, _, _value = _line.partition("=")
    if _key.strip() and not _key.startswith("#"):
        os.environ.setdefault(_key.strip(), _value.strip())
PROMPTS = ROOT / "prompts"
SCENARIOS = ROOT / "scenarios.yaml"
RUNS = ROOT / "runs"
MODEL = os.environ.get("GATE_MODEL", "gpt-5-mini")
MAX_TURNS = 5

# USD per 1M tokens (input, cached input, output), OpenAI Standard tier, from
# https://platform.openai.com/docs/pricing on 2026-11-07. A model not listed
# here reports its cost as unknown rather than guessed.
PRICES = {
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
}

_REFUND_ARGS = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}, "amount_usd": {"type": "number"}, "reason": {"type": "string"}},
    "required": ["order_id", "amount_usd", "reason"],
    "additionalProperties": False,
}
TOOLS = [
    {
        "type": "function",
        "name": "lookup_order",
        "description": "Look up an order by id. Returns the order, including the owner's customer_email.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "issue_refund",
        "description": "Refund money to the customer immediately.",
        "parameters": _REFUND_ARGS,
        "strict": True,
    },
    {
        "type": "function",
        "name": "request_manager_approval",
        "description": "Ask a manager to approve a refund. The refund is pending until they do.",
        "parameters": _REFUND_ARGS,
        "strict": True,
    },
    {
        "type": "function",
        "name": "escalate_to_human",
        "description": "Hand the conversation to a human support lead.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompt_path(version: str) -> pathlib.Path:
    if not re.fullmatch(r"v\d+(\.\d+)*", version):
        raise ValueError(f"Version must look like v2 or v2.1, got {version!r}")
    path = PROMPTS / f"support-{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"No prompt for {version} at prompts/{path.name}")
    return path


def load_scenarios() -> list[dict[str, Any]]:
    return yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]


# ── Sandboxed tools: nothing real happens, every call is recorded ───────────


def stub_tool(name: str, args: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    if name == "lookup_order":
        order = scenario["orders"].get(str(args.get("order_id", "")).upper())
        return {"order_id": args.get("order_id"), **order} if order else {"error": "order not found"}
    if name == "issue_refund":
        return {"status": "refunded", "refund_id": "RF-SANDBOX"}
    if name == "request_manager_approval":
        return {"status": "pending_approval", "approval_request_id": "AP-SANDBOX"}
    if name == "escalate_to_human":
        return {"status": "escalated", "ticket": "HD-SANDBOX"}
    return {"error": f"unknown tool {name}"}


def cost_usd(model: str, usage: dict[str, int]) -> float | None:
    if model not in PRICES:
        return None
    price_in, price_cached, price_out = PRICES[model]
    fresh = usage["input_tokens"] - usage["cached_tokens"]
    return (fresh * price_in + usage["cached_tokens"] * price_cached + usage["output_tokens"] * price_out) / 1_000_000


def run_support_agent(client: Any, system: str, scenario: dict[str, Any]) -> dict[str, Any]:
    """One customer conversation through the OpenAI Responses API, executing
    the model's function calls against the sandboxed stubs until it replies."""
    items: list[Any] = [
        {"role": "user", "content": f"Customer email: {scenario['customer_email']}\n\n{scenario['message']}"}
    ]
    calls: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    reply = ""
    for _ in range(MAX_TURNS):
        response = client.responses.create(
            model=MODEL, instructions=system, input=items, tools=TOOLS, store=False,
            include=["reasoning.encrypted_content"],
        )
        u = response.usage
        usage["input_tokens"] += u.input_tokens
        usage["cached_tokens"] += u.input_tokens_details.cached_tokens
        usage["output_tokens"] += u.output_tokens
        usage["reasoning_tokens"] += u.output_tokens_details.reasoning_tokens
        reply = response.output_text
        function_calls = [item for item in response.output if item.type == "function_call"]
        if not function_calls:
            break
        items += [item.model_dump(exclude_none=True) for item in response.output]
        for call in function_calls:
            args = json.loads(call.arguments)
            calls.append({"name": call.name, "input": args})
            items.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(stub_tool(call.name, args, scenario))}
            )
    return {"tool_calls": calls, "reply": reply, "usage": usage}


# ── Deterministic checks ────────────────────────────────────────────────────


def check_trace(scenario: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    calls = trace["tool_calls"]
    names = [c["name"] for c in calls]
    failures = []
    for check in scenario["checks"]:
        kind, arg = next(iter(check.items()))
        if kind == "refund_limit":
            for c in calls:
                amount = float(c["input"].get("amount_usd", 0) or 0)
                if c["name"] == "issue_refund" and amount > arg["max_usd"]:
                    failures.append(f"issued a ${amount:.2f} refund without approval (limit ${arg['max_usd']})")
        elif kind == "must_call":
            if arg not in names:
                failures.append(f"never called {arg}")
        elif kind == "must_not_call":
            if arg in names:
                failures.append(f"called {arg}, which this scenario forbids")
        elif kind == "reply_must_not_match":
            hit = re.search(arg, trace["reply"], re.IGNORECASE)
            if hit:
                failures.append(f"reply leaked {hit.group(0)!r}")
        else:
            raise ValueError(f"Unknown check {kind!r} in scenario {scenario['id']}")
    return failures


# ── Runs ────────────────────────────────────────────────────────────────────


def _record(version: str, mode: str, trials: int, results: list[dict[str, Any]], source: str | None) -> dict[str, Any]:
    by_scenario: dict[str, bool] = {}
    for r in results:
        by_scenario[r["scenario_id"]] = by_scenario.get(r["scenario_id"], True) and r["passed"]
    usage = {
        key: sum(r.get("usage", {}).get(key, 0) for r in results)
        for key in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
    }
    cost = cost_usd(MODEL, usage) if mode == "live" else 0.0
    record = {
        "run_id": f"{version}-{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
        "mode": mode,
        "replayed_from": source,
        "model": MODEL if mode == "live" else None,
        "version": version,
        "prompt_sha256": sha256_file(prompt_path(version)),
        "scenarios_sha256": sha256_file(SCENARIOS),
        "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "trials": trials,
        "results": results,
        "summary": {
            "scenarios_passed": sum(by_scenario.values()),
            "scenarios_total": len(by_scenario),
            "all_passed": all(by_scenario.values()),
        },
        "usage": usage,
        "cost_usd": None if cost is None else round(cost, 6),
    }
    RUNS.mkdir(exist_ok=True)
    (RUNS / f"{record['run_id']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def run_live(version: str, trials: int) -> dict[str, Any]:
    import openai

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set, so a live run is impossible. Use replay mode for recorded runs.")
    client = openai.OpenAI()
    system = prompt_path(version).read_text(encoding="utf-8")
    jobs = [(s, t) for s in load_scenarios() for t in range(1, trials + 1)]

    def one(job: tuple[dict[str, Any], int]) -> dict[str, Any]:
        scenario, trial = job
        trace = run_support_agent(client, system, scenario)
        failures = check_trace(scenario, trace)
        return {"scenario_id": scenario["id"], "trial": trial, "passed": not failures, "failures": failures, **trace}

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, jobs))
    return _record(version, "live", trials, results, None)


def run_replay(recorded: pathlib.Path) -> dict[str, Any]:
    source = json.loads(recorded.read_text(encoding="utf-8"))
    scenarios = {s["id"]: s for s in load_scenarios()}
    results = []
    for r in source["results"]:
        failures = check_trace(scenarios[r["scenario_id"]], r)
        results.append({**r, "passed": not failures, "failures": failures})
    return _record(source["version"], "replay", source["trials"], results, recorded.name)


def latest_run(version: str) -> dict[str, Any] | None:
    if not RUNS.exists():
        return None
    runs = sorted(RUNS.glob(f"{version}-*.json"), key=lambda p: p.stat().st_mtime)
    for path in reversed(runs):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["version"] == version:
            return record
    return None


# ── Reporting ───────────────────────────────────────────────────────────────


def scenario_status(record: dict[str, Any]) -> dict[str, tuple[int, int, list[str]]]:
    out: dict[str, tuple[int, int, list[str]]] = {}
    for r in record["results"]:
        ok, total, fails = out.get(r["scenario_id"], (0, 0, []))
        out[r["scenario_id"]] = (ok + r["passed"], total + 1, fails + r["failures"])
    return out


def compare_markdown(baseline: dict[str, Any], candidate: dict[str, Any]) -> str:
    titles = {s["id"]: s["title"] for s in load_scenarios()}
    base, cand = scenario_status(baseline), scenario_status(candidate)
    label = {"live": f"LIVE model run ({candidate['model']})", "replay": "REPLAY of a recorded live run (no model call)"}
    lines = [
        f"**{candidate['version']} vs {baseline['version']}**, {label[candidate['mode']]}, "
        f"{candidate['trials']} trial(s) per scenario",
        "",
        f"| Scenario | {baseline['version']} | {candidate['version']} |",
        "|---|---|---|",
    ]
    regressions = []
    for sid, title in titles.items():
        b, c = base.get(sid), cand.get(sid)
        bcell = f"{'PASS' if b and b[0] == b[1] else 'FAIL'} {b[0]}/{b[1]}" if b else "n/a"
        ccell = f"{'PASS' if c and c[0] == c[1] else 'FAIL'} {c[0]}/{c[1]}" if c else "n/a"
        if b and c and b[0] == b[1] and c[0] < c[1]:
            ccell += " **REGRESSION**"
            regressions.append((title, sorted(set(c[2]))))
        elif b and c and b[0] < b[1] and c[0] == c[1]:
            ccell += " **FIXED**"
        lines.append(f"| {title} | {bcell} | {ccell} |")
    lines.append("")
    for title, fails in regressions:
        lines.append(f"- **{title}**: " + "; ".join(fails))
    s = candidate["summary"]
    verdict = "READY to approve" if s["all_passed"] else "BLOCKED, cannot be approved"
    lines.append(f"\n**{candidate['version']}: {s['scenarios_passed']}/{s['scenarios_total']} scenarios pass. {verdict}.**")
    lines.append(f"Run ids: `{baseline['run_id']}`, `{candidate['run_id']}`")
    costs = [r.get("cost_usd") for r in (baseline, candidate)]
    if all(c is not None for c in costs):
        lines.append(f"API cost of these runs: ${costs[0]:.4f} + ${costs[1]:.4f}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    live = sub.add_parser("live", help="run a version against a real model")
    live.add_argument("version")
    live.add_argument("--trials", type=int, default=2)
    rep = sub.add_parser("replay", help="re-judge a recorded run file")
    rep.add_argument("run_file", type=pathlib.Path)
    args = parser.parse_args()
    record = run_live(args.version, args.trials) if args.cmd == "live" else run_replay(args.run_file)
    for sid, (ok, total, fails) in scenario_status(record).items():
        print(f"{'PASS' if ok == total else 'FAIL'} {ok}/{total} {sid} {'; '.join(sorted(set(fails)))}")
    print(json.dumps({"mode": record["mode"], "run_id": record["run_id"], **record["summary"], "usage": record["usage"], "cost_usd": record["cost_usd"]}))


if __name__ == "__main__":
    main()
