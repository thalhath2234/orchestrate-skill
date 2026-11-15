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
API overload and rate limits are retried with backoff (claude_cli), never blamed on a model.
Exit codes: 0 ok, 2 bad usage, 3 environment broken, 4 gave up (needs a human), 7 budget.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_cli  # noqa: E402
import router  # noqa: E402
import schemas  # noqa: E402

# Infrastructure signatures only. Test names and assertions ("TestRateLimit", "expected 429",
# "Cannot find module") are the agent's business and must not stop a task as "environment".
ENV_PATTERNS = re.compile(
    r"connection refused|ECONNREFUSED|could not connect to server|no such host|"
    r"dial tcp [^\n]*: connect: |address already in use|EADDRINUSE|no space left on device|"
    r"network is unreachable|Temporary failure in name resolution|cannot allocate memory",
    re.I)

# How a claude error (after claude_cli's own retries) is counted.
ERROR_CAUSE = {"timeout": "unknown", "max_turns": "effort", "overloaded": "environment",
               "rate_limit": "environment", "api": "environment", "exec": "unknown",
               "unparseable": "unknown"}


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


def _command_words(cmd: str) -> list[str]:
    """The words of the last simple command after `cd x &&` and `VAR=x` / `env VAR=x` prefixes."""
    try:
        words = shlex.split(cmd.split("&&")[-1])
    except ValueError:
        return []
    if words and words[0] == "env":
        words = words[1:]
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    return words


def env_signature(vout: str, verify_cmds: list[str]) -> str | None:
    """An infrastructure failure in verify output, or a verify command that can't run at all."""
    tail = vout[-4000:]
    m = ENV_PATTERNS.search(tail)
    if m:
        return m.group(0).strip()
    for c in verify_cmds:
        words = _command_words(c)
        if not words:
            continue
        tool = Path(words[0]).name
        if re.search(rf"(^|[\s:]){re.escape(tool)}: (command )?not found", tail, re.M):
            return f"{tool}: command not found"
        if tool == "make" and len(words) > 1 and not words[1].startswith("-"):
            if re.search(rf"No rule to make target ['`]{re.escape(words[1])}'", tail):
                return f"no make target {words[1]}"
    return None


def claude(prompt: str, model: str, effort: str, cwd: Path, *, perm: str,
           tools: list[str], resume: str | None, timeout: int, settings: dict | None = None,
           schema: dict | None = None, budget_usd: float | None = None,
           extra_args: list[str] | None = None) -> dict:
    return claude_cli.call(prompt, model, effort, cwd, perm=perm, tools=tools, resume=resume,
                           timeout=timeout, settings=settings, schema=schema,
                           budget_usd=budget_usd, extra_args=extra_args)


def git(repo: Path, *args: str) -> str:
    ok, out = sh("git " + " ".join(shlex.quote(a) for a in args), repo, 60)
    return out if ok else ""


CLASSIFIER_PROMPT = """FAILURE_CLASSIFIER
A coding agent ({config}) failed a task twice. Decide WHY, using only the evidence.

Causes, with the examples that separate the confusable ones:
- environment: tooling, services or the machine broke, unrelated to the agent's code.
  e.g. the database is down; a verify command is missing from the Makefile; the disk is full.
- context: the agent lacked or was misled by repo knowledge it could not reasonably infer.
  e.g. it put handlers in handlers/ but this repo registers them in internal/http/routes.go;
  the task named a file that doesn't exist; a stale AGENTS.md sent it the wrong way.
- effort: it cut corners. It skipped files, didn't run or read the tests, stopped partway,
  ignored a compiler error it could see, or left a TODO.
  e.g. the final message says "done" while verify shows a failing test it never ran.
- capability: it had the context and tried properly, but the logic is still wrong.
  e.g. it read the right files, ran the tests and iterated, yet the concurrency fix still races.

If several apply, choose the first in this order: environment > context > effort > capability.
A failing test the agent never ran is effort, not capability. A test about rate limits,
timeouts or HTTP 429 is code under test, not an environment problem.

TASK:
{task}

AGENT'S FINAL MESSAGE:
{final}

DIFF STAT:
{stat}

VERIFY OUTPUT (tail):
{tail}

You may read files in the repo. Reply with the JSON object: cause, evidence (one sentence),
agents_md (null, or {{"file": "<repo-relative AGENTS.md path>", "text": "<one-line note that
would have prevented this>"}}, only when cause is context)."""


def classifier_cfg() -> tuple[str, str]:
    m, _, e = os.environ.get("ROUTER_CLASSIFIER", "sonnet/low").partition("/")
    return m, e or "low"


def classify(task: str, rung: int, worker: dict, vout: str, repo: Path, timeout: int,
             verify_cmds: list[str] | None = None) -> dict:
    """Return {"cause", "evidence", "agents_md": {"file","text"}|None}."""
    kind = worker.get("error_kind") or ("timeout" if worker.get("timeout") else None)
    if kind:
        return {"cause": ERROR_CAUSE.get(kind, "unknown"),
                "evidence": f"claude {kind}: {str(worker.get('result'))[:300]}"}
    if worker.get("is_error"):
        return {"cause": "unknown", "evidence": f"claude error: {str(worker.get('result'))[:300]}"}
    sig = env_signature(vout, verify_cmds or [])
    if sig:
        return {"cause": "environment", "evidence": f"verify output matched {sig!r}"}
    prompt = CLASSIFIER_PROMPT.format(
        config="/".join(router.cfg(rung)), task=task, final=str(worker.get("result", ""))[-3000:],
        stat=git(repo, "diff", "--stat")[-1500:] or "(none)", tail=vout[-4000:])
    res = claude(prompt, *classifier_cfg(), repo, perm="plan", tools=[], resume=None,
                 timeout=timeout, schema=schemas.CLASSIFIER)
    out = res.get("structured") or {}
    if out.get("cause") not in router.CAUSES:
        out = {"cause": "unknown", "evidence": f"classifier unparseable: {str(res.get('result'))[:200]}"}
    out["classifier_cost"] = res.get("total_cost_usd")
    out["classifier_metrics"] = res.get("metrics")
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
                    # No bare Edit/Write: that would allow edits ANYWHERE. acceptEdits already
                    # auto-approves edits inside the repo/worktree and nothing else.
                    default=["Read", "Glob", "Grep", "Bash(make *)",
                             "Bash(go *)", "Bash(cargo *)", "Bash(pnpm *)", "Bash(npm *)",
                             "Bash(npx *)", "Bash(yarn *)", "Bash(bun *)", "Bash(node *)",
                             "Bash(python3 *)", "Bash(pytest *)", "Bash(uv *)",
                             "Bash(git diff*)", "Bash(git status*)", "Bash(git log*)",
                             "Bash(git show*)", "Bash(rg *)", "Bash(ls *)", "Bash(cat *)",
                             "Bash(head *)", "Bash(tail *)", "Bash(wc *)", "Bash(find *)",
                             "Bash(mkdir *)", "Bash(cd *)", "Bash(test *)"])
    ap.add_argument("--sandbox", action="store_true",
                    help="OS-level sandbox for the worker's shell commands (writes limited to the "
                         "repo; may break tools that write caches elsewhere, e.g. Go)")
    ap.add_argument("--extra-tools", nargs="*", default=[],
                    help="additional --allowedTools entries, e.g. 'Bash(docker compose *)'")
    ap.add_argument("--baseline", action="append",
                    help="pre-task health check (repeatable; default: the --verify commands). "
                         "Use when --verify includes a test that is SUPPOSED to fail now")
    ap.add_argument("--no-baseline", action="store_true", help="skip the pre-task health check")
    ap.add_argument("--verify-timeout", type=int, default=900)
    ap.add_argument("--claude-timeout", type=int, default=3600)
    ap.add_argument("--report-schema", help="JSON Schema for the worker's final report "
                                            "(structured output); the parsed report is in 'report'")
    ap.add_argument("--budget-usd", type=float, help="stop when this run has spent this much")
    ap.add_argument("--exclude-dynamic-prompt", action="store_true",
                    help="pass --exclude-dynamic-system-prompt-sections (cache reuse across worktrees)")
    ap.add_argument("--dry-run", action="store_true", help="only print the routing decision")
    a = ap.parse_args(argv)
    repo = a.repo.expanduser().resolve()
    router.validate_type(a.type)
    # the worker is told to run the verify commands itself, so it must be allowed to
    a.allowed_tools += a.extra_tools + [f"Bash({c})" for c in a.verify]
    task_id = uuid.uuid4().hex[:10]
    report_schema = json.loads(a.report_schema) if a.report_schema else None
    extra = ["--exclude-dynamic-system-prompt-sections"] if a.exclude_dynamic_prompt else []

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

    settings = {"permissions": {"blockWritesOutsideWorkingDirectories": True}}
    if a.sandbox:
        settings["sandbox"] = {"enabled": True, "autoAllowBashIfSandboxed": True}
    rung, probe, hint = decision["rung"], decision["probe"], ""
    summary = {"task_id": task_id, "type": a.type, "tier": a.tier, "steps": [], "proposals": []}
    escalations, spent = 0, 0.0

    def remaining():
        return None if a.budget_usd is None else a.budget_usd - spent

    while True:
        model, effort = router.cfg(rung)
        session, worker, vout, ok = None, {}, "", False
        cost = 0.0
        metrics: dict = {}
        for attempt in range(1, a.attempts + 1):
            prompt = worker_prompt(a.task, repo, a.verify, hint) if attempt == 1 else (
                "Verification still fails. Fix it. Output:\n" + vout[-5000:])
            log(f"attempt {attempt}/{a.attempts} at {model}/{effort}")
            worker = claude(prompt, model, effort, repo, perm=a.permission_mode,
                            tools=a.allowed_tools, resume=session, timeout=a.claude_timeout,
                            settings=settings, schema=report_schema, budget_usd=remaining(),
                            extra_args=extra)
            session = worker.get("session_id") or session
            c = worker.get("total_cost_usd") or 0.0
            cost += c
            spent += c
            for k, v in (worker.get("metrics") or {}).items():
                if isinstance(v, (int, float)):
                    metrics[k] = metrics.get(k, 0) + v
            if worker.get("is_error"):
                vout = str(worker.get("result"))
                break
            ok, vout = verify(a.verify, repo, a.verify_timeout)
            if ok:
                break
        step = {"config": f"{model}/{effort}", "probe": probe, "ok": ok, "cost_usd": round(cost, 6),
                "metrics": metrics}
        summary["steps"].append(step)

        if ok:
            summary["proposals"] += router.record(
                task_id=task_id, task_type=a.type, tier=a.tier, rung=rung, ok=True, cause=None,
                model_id=worker.get("model_id"), probe=probe, cost_usd=cost)
            log(f"PASS at {model}/{effort}")
            break

        if worker.get("error_kind") == "budget":
            step.update(cause="budget", evidence="budget reached")
            summary["result"] = "budget"
            log("budget reached -> stopping")
            break

        c = classify(a.task, rung, worker, vout, repo, a.claude_timeout, a.verify)
        spent += c.get("classifier_cost") or 0
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
        if a.budget_usd is not None and spent >= a.budget_usd:
            summary["result"] = "budget"
            break
        if new != rung:
            hint = (hint.split("\nA previous attempt")[0] +
                    "\nA previous attempt by a weaker configuration left changes in the "
                    "working tree; review them, keep what is right, revert what is wrong.")
        rung, probe = new, False

    summary.setdefault("result", "ok")
    summary["final_message"] = str(worker.get("result", ""))[-6000:]
    if isinstance(worker.get("structured"), dict):
        summary["report"] = worker["structured"]
    summary["proposals"] = [{"id": p["id"], "kind": p["kind"], "reason": p["reason"]}
                            for p in summary["proposals"]]
    print(json.dumps(summary, indent=2))
    if summary["proposals"]:
        log(f"{len(summary['proposals'])} proposal(s) pending: router.py proposals")
    return {"ok": 0, "environment": 3, "budget": 7}.get(summary["result"], 4)


if __name__ == "__main__":
    sys.exit(main())
