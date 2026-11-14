#!/usr/bin/env python3
"""Orchestrator: run one coding task through Claude Code with learned model routing.

  run_task.py --type bugfix:api --tier T1 --repo ~/Dev/timely-app \
      --verify "make test-api" "Fix the 500 on GET /tasks when due_date is null"

Flow
  1. baseline: run --verify before touching anything; if it already fails the
     environment is broken -> stop (no tokens spent, no blame on any model).
  2. route:    router.route() picks the starting (model, effort) from the tier
     default plus any APPROVED learned floor (occasionally probes one rung lower).
  3. attempt:  up to --attempts tries at that config (2nd try resumes the same
     Claude session with the verify output). Pass -> record success, done.
  4. failure:  classify the cause (environment / context / effort / capability),
     record ONE failure for this task at this config, then for THIS task:
       environment -> stop
       context     -> propose an AGENTS.md note, retry once with the hint
       effort      -> same model, more effort
       capability  -> next model
     Policy for FUTURE tasks never changes here: router.py only files
     proposals, which you approve/reject.
Exit codes: 0 ok, 2 bad usage, 3 environment broken, 4 gave up (needs a human).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import router  # noqa: E402

CLAUDE = os.environ.get("ROUTER_CLAUDE_BIN", "claude")
CLASSIFIER = ("sonnet", "low")
ENV_PATTERNS = re.compile(
    r"connection refused|ECONNREFUSED|could not connect to server|no such host|"
    r"dial tcp .*: connect|address already in use|EADDRINUSE|no space left on device|"
    r"rate.?limit|overloaded_error|\b(429|529)\b|ETIMEDOUT|network is unreachable|"
    r"command not found|No rule to make target|Cannot find module '[^.]",
    re.I)


def log(msg: str) -> None:
    print(f"[run_task] {msg}", file=sys.stderr, flush=True)


def sh(cmd: str, cwd: Path, timeout: int) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        return False, f"TIMEOUT after {timeout}s: {cmd}\n{e.stdout or ''}{e.stderr or ''}"


def verify(cmds: list[str], cwd: Path, timeout: int) -> tuple[bool, str]:
    out = []
    for c in cmds:
        ok, text = sh(c, cwd, timeout)
        out.append(f"$ {c}\n{text[-6000:]}")
        if not ok:
            return False, "\n".join(out)
    return True, "\n".join(out)


def claude(prompt: str, model: str, effort: str, cwd: Path, *, perm: str,
           tools: list[str], resume: str | None, timeout: int) -> dict:
    cmd = [CLAUDE, "-p", prompt, "--model", model, "--effort", effort,
           "--output-format", "json", "--permission-mode", perm]
    if tools:
        cmd += ["--allowedTools", *tools]
    if resume:
        cmd += ["--resume", resume]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"is_error": True, "result": f"claude timed out after {timeout}s", "timeout": True}
    try:
        data = json.loads(p.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"is_error": True, "result": (p.stdout + p.stderr)[-2000:]}
    usage = data.get("modelUsage") or {}
    data["model_id"] = next((k for k in usage if model in k), next(iter(usage), None))
    return data


def git(repo: Path, *args: str) -> str:
    ok, out = sh("git " + " ".join(shlex.quote(a) for a in args), repo, 60)
    return out if ok else ""


def classify(task: str, rung: int, worker: dict, vout: str, repo: Path, timeout: int) -> dict:
    """Return {"cause", "evidence", "agents_md": {"file","text"}|None}."""
    if worker.get("timeout"):
        return {"cause": "unknown", "evidence": "worker timed out (looping or task too big?)"}
    if worker.get("is_error"):
        return {"cause": "environment", "evidence": f"claude error: {str(worker.get('result'))[:300]}"}
    tail = vout[-4000:]
    m = ENV_PATTERNS.search(tail)
    if m:
        return {"cause": "environment", "evidence": f"verify output matched {m.group(0)!r}"}
    prompt = f"""FAILURE_CLASSIFIER
A coding agent ({'/'.join(router.cfg(rung))}) failed a task twice. Decide WHY, using only the evidence.

Causes:
- environment: broken tooling/services/flaky tests, unrelated to the agent's code.
- context: the agent lacked or was misled by repo knowledge (missing/stale AGENTS.md, ambiguous task, wrong file guessed).
- effort: agent cut corners - skipped files, didn't run or read tests, stopped partway, ignored errors.
- capability: had the needed context, tried properly, still produced wrong logic.

TASK:
{task}

AGENT'S FINAL MESSAGE:
{str(worker.get('result', ''))[-3000:]}

DIFF STAT:
{git(repo, 'diff', '--stat')[-1500:] or '(none)'}

VERIFY OUTPUT (tail):
{tail}

You may read files in the repo. Reply with ONLY a JSON object:
{{"cause": "...", "evidence": "<one sentence>", "agents_md": null | {{"file": "<repo-relative AGENTS.md path>", "text": "<one-line note that would have prevented this>"}}}}
Only fill agents_md when cause is context."""
    res = claude(prompt, *CLASSIFIER, repo, perm="plan", tools=[], resume=None, timeout=timeout)
    m = re.search(r"\{.*\}", str(res.get("result", "")), re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        out = {}
    if out.get("cause") not in router.CAUSES:
        out = {"cause": "unknown", "evidence": f"classifier unparseable: {str(res.get('result'))[:200]}"}
    out["classifier_cost"] = res.get("total_cost_usd")
    return out


def worker_prompt(task: str, repo: Path, verify_cmds: list[str], hint: str = "") -> str:
    v = "\n".join(f"  {c}" for c in verify_cmds)
    return f"""{task}

Working directory: {repo}. Read the nearest AGENTS.md before editing.
Done means these commands pass (run them yourself before finishing):
{v}
Do not commit. Keep changes minimal and in scope.{hint}"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", help="the task prompt")
    ap.add_argument("--type", required=True, help="<category>:<area>, e.g. bugfix:api")
    ap.add_argument("--tier", required=True, choices=router.TIER_DEFAULT)
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--verify", action="append", required=True, help="command that must pass (repeatable)")
    ap.add_argument("--attempts", type=int, default=2, help="tries per config before escalating")
    ap.add_argument("--max-escalations", type=int, default=2)
    ap.add_argument("--permission-mode", default="acceptEdits")
    ap.add_argument("--allowed-tools", nargs="*",
                    default=["Read", "Edit", "Write", "Glob", "Grep",
                             "Bash(make *)", "Bash(go *)", "Bash(pnpm *)", "Bash(git diff*)",
                             "Bash(git status*)", "Bash(rg *)", "Bash(ls *)"])
    ap.add_argument("--baseline", action="append",
                    help="pre-task health check (repeatable; default: the --verify commands). "
                         "Use when --verify includes a test that is SUPPOSED to fail now")
    ap.add_argument("--no-baseline", action="store_true", help="skip the pre-task health check")
    ap.add_argument("--verify-timeout", type=int, default=900)
    ap.add_argument("--claude-timeout", type=int, default=3600)
    ap.add_argument("--dry-run", action="store_true", help="only print the routing decision")
    a = ap.parse_args(argv)
    repo = a.repo.expanduser().resolve()
    router.validate_type(a.type)
    # the worker is told to run the verify commands itself, so it must be allowed to
    a.allowed_tools += [f"Bash({c})" for c in a.verify]
    task_id = uuid.uuid4().hex[:10]

    decision = router.route(a.type, a.tier)
    log(f"task {task_id} {a.type} {a.tier} -> {decision['model']}/{decision['effort']} ({decision['source']})")
    if a.dry_run:
        print(json.dumps(decision))
        return 0

    if not a.no_baseline:
        ok, out = verify(a.baseline or a.verify, repo, a.verify_timeout)
        if not ok:
            log("baseline check FAILS before any change -> environment problem; not starting.")
            print(out[-3000:], file=sys.stderr)
            return 3
    if git(repo, "status", "--porcelain").strip():
        log("warning: repo has uncommitted changes; consider running in a worktree")

    rung, probe, hint = decision["rung"], decision["probe"], ""
    summary = {"task_id": task_id, "type": a.type, "tier": a.tier, "steps": [], "proposals": []}
    escalations = 0
    while True:
        model, effort = router.cfg(rung)
        session, worker, vout, ok = None, {}, "", False
        for attempt in range(1, a.attempts + 1):
            prompt = worker_prompt(a.task, repo, a.verify, hint) if attempt == 1 else (
                "Verification still fails. Fix it. Output:\n" + vout[-5000:])
            log(f"attempt {attempt}/{a.attempts} at {model}/{effort}")
            worker = claude(prompt, model, effort, repo, perm=a.permission_mode,
                            tools=a.allowed_tools, resume=session, timeout=a.claude_timeout)
            session = worker.get("session_id") or session
            if worker.get("is_error"):
                vout = str(worker.get("result"))
                break
            ok, vout = verify(a.verify, repo, a.verify_timeout)
            if ok:
                break
        cost = worker.get("total_cost_usd")
        step = {"config": f"{model}/{effort}", "probe": probe, "ok": ok, "cost_usd": cost}
        summary["steps"].append(step)

        if ok:
            summary["proposals"] += router.record(
                task_id=task_id, task_type=a.type, tier=a.tier, rung=rung, ok=True, cause=None,
                model_id=worker.get("model_id"), probe=probe, cost_usd=cost)
            log(f"PASS at {model}/{effort}")
            break

        c = classify(a.task, rung, worker, vout, repo, a.claude_timeout)
        step.update(cause=c["cause"], evidence=c.get("evidence"),
                    classifier_cost_usd=c.get("classifier_cost"))
        log(f"FAIL at {model}/{effort}: {c['cause']} - {c.get('evidence')}")
        summary["proposals"] += router.record(
            task_id=task_id, task_type=a.type, tier=a.tier, rung=rung, ok=False,
            cause=c["cause"], model_id=worker.get("model_id"), probe=probe, cost_usd=cost,
            note=c.get("evidence", ""))

        if c["cause"] == "environment":
            summary["result"] = "environment"
            break
        if c["cause"] == "context":
            note = (c.get("agents_md") or {}).get("text") or c.get("evidence", "")
            if c.get("agents_md"):
                f = repo / c["agents_md"]["file"]
                summary["proposals"].append(router.propose_context(
                    a.type, str(f), note, c.get("evidence", "")))
            hint = f"\nNote from a previous failed attempt: {note}"
            new = rung  # same model; the problem was knowledge, not capability
        else:
            new = router.next_rung(rung, "effort" if c["cause"] == "effort" else "capability")
            if probe:  # a failed probe goes straight back to the learned floor
                new = max(new, decision["rung"] + 1)
        escalations += 1
        if escalations > a.max_escalations or (new == rung and c["cause"] != "context"):
            summary["result"] = "gave_up"
            log("out of escalations / top of ladder -> needs a human")
            break
        if new != rung:
            hint = (hint.split("\nA previous attempt")[0] +
                    "\nA previous attempt by a weaker configuration left changes in the "
                    "working tree; review them, keep what is right, revert what is wrong.")
        rung, probe = new, False

    summary.setdefault("result", "ok")
    summary["proposals"] = [{"id": p["id"], "kind": p["kind"], "reason": p["reason"]}
                            for p in summary["proposals"]]
    print(json.dumps(summary, indent=2))
    if summary["proposals"]:
        log(f"{len(summary['proposals'])} proposal(s) pending: router.py proposals")
    return {"ok": 0, "environment": 3}.get(summary["result"], 4)


if __name__ == "__main__":
    sys.exit(main())
