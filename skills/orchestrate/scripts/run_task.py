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
Exit codes: 0 ok, 2 bad usage, 3 environment broken, 4 gave up (needs a human), 7 budget,
9 refused by a safety classifier (see refusal_category; not routing evidence).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_cli  # noqa: E402
import jail  # noqa: E402
import models  # noqa: E402
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
               "unparseable": "unknown", "cut_off": "unknown"}


def log(msg: str) -> None:
    print(f"[run_task] {msg}", file=sys.stderr, flush=True)


def sh(cmd: str, cwd: Path, timeout: int, confine: Path | None = None) -> tuple[bool, str]:
    try:
        p = subprocess.run(jail.shell(cmd, cwd, confine), cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return p.returncode == 0, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        out, err = ((x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")) for x in (e.stdout, e.stderr))
        return False, f"TIMEOUT after {timeout}s: {cmd}\n{out}{err}"


def verify(cmds: list[str], cwd: Path, timeout: int, confine: Path | None = None,
           trusted: list[str] | tuple = ()) -> tuple[bool, str]:
    """`confine`: run in the jail (they run code a worker wrote). `trusted`: the harness's own
    commands (the scope check), which write the state the jail protects."""
    out = []
    for c in cmds:
        ok, text = sh(c, cwd, timeout, None if c in trusted else confine)
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
           extra_args: list[str] | None = None, only: list[str] | None = None,
           confine: Path | None = None) -> dict:
    return claude_cli.call(prompt, model, effort, cwd, perm=perm, tools=tools, resume=resume,
                           timeout=timeout, settings=settings, schema=schema,
                           budget_usd=budget_usd, extra_args=extra_args, only=only, confine=confine)


def git(repo: Path, *args: str) -> str:
    ok, out = sh(" ".join(shlex.quote(a) for a in [*jail.GIT, *args]), repo, 60)
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

You may read files in the repo. Reply with the JSON object: evidence first (one or two
sentences: what the evidence shows), then cause, then agents_md (null, or {{"file": "<repo-relative AGENTS.md path>", "text": "<one-line note that
would have prevented this>"}}, only when cause is context)."""


def classifier_cfg() -> tuple[str, str]:
    """Haiku 5.5 at low, Anthropic's pick for classification (ticket-routing guide)."""
    spec = os.environ.get("ROUTER_CLASSIFIER")
    return models.parse(spec) if spec else models.role("classifier")


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
    res = claude(prompt, *classifier_cfg(), repo, perm="plan", tools=[], only=[], resume=None,
                 timeout=timeout, schema=schemas.CLASSIFIER,
                 extra_args=["--exclude-dynamic-system-prompt-sections"])
    out = res.get("structured") or {}
    if out.get("cause") not in router.CAUSES:
        out = {"cause": "unknown", "evidence": f"classifier unparseable: {str(res.get('result'))[:200]}"}
    out["classifier_cost"] = res.get("total_cost_usd")
    out["classifier_metrics"] = res.get("metrics")
    return out


def done_gaps(report, ids: list[str]) -> list[str]:
    """Definition-of-done items the worker's own report doesn't show as met with evidence."""
    rows = {str(r.get("id")): r for r in ((report or {}).get("done_check") or []) if isinstance(r, dict)}
    gaps = []
    for i in ids:
        r = rows.get(i)
        if r is None:
            gaps.append(f"{i}: not reported")
        elif not r.get("met"):
            gaps.append(f"{i}: not met ({str(r.get('evidence', ''))[:200]})")
        elif not schemas.has_evidence(r.get("evidence")):
            gaps.append(f"{i}: marked met without evidence (name a command and its result, a test or file:line)")
    return gaps


# The advisor tool only helps when the worker actually calls it: Claude decides when, and on one
# Anthropic benchmark Opus rarely did and scored 7 points below Opus alone. So say when [ADVISOR].
CONSULT = ("\nYou have an advisor (a stronger model) available as a tool. Consult the advisor: "
           "before you commit to an approach, when the same error comes back a second time, and "
           "before you write your final report. Apply its guidance unless the code or a test "
           "contradicts it; then say so in your report.")
CONSULT_RETRY = " If this is the same failure as before, consult the advisor before changing course."


# Worker instructions adapted from Anthropic's prompting guides (P9). Each paragraph names its
# source; measure a change with `evals/run.py --role worker` before keeping it.
#
# [Opus 5.5 "Unattended agentic runs", Fable 5.2 "Finish the whole task"] Workers run unattended
# at medium effort, where both guides see turns end early on a progress note or an offer to go on.
FINISH = """Nobody is watching this session to answer questions: a message without a tool call ends your
work, and the task is then judged as you left it. So carry the whole task through. Don't end on a
summary that announces the next step, an offer to continue, or a list of decisions that don't
block you; do the next step instead. Ordinary obstacles (a failing command, a timeout, an empty
result) are yours to diagnose and work through. Stop early only for something deliberately
withheld from you, such as a path you don't own, and then say plainly what it was."""
# [Sonnet 5.5 and Haiku 5.5 "Verification on coding tasks"] for a worker override to a smaller
# model, and cheap on Opus.
VERIFY = """Before you report the work done, run a real check that exercises the change: the commands below
and the tests nearest to what you changed. A syntax-only check, or a command that failed to start,
doesn't count. If the project's declared dependencies are missing, install them with its own
package manager from the existing lockfile, without changing the lockfile and never with sudo or
the system package manager. If no real check can run, say which one and why instead of reporting
the change as done."""
# [Fable 5.1 "Keep changes and tests to what the task asks for"; Fable 5 "Ground progress claims"]
SCOPE = """Do what the task asks, completely, and no more. If you find a pre-existing bug, a performance
problem or behaviour the task doesn't mention, leave it unless the task can't work without the
fix, and report it (in a job, as an `fyi` open issue). Where the task is ambiguous, implement the
reading its wording and the surrounding code support best, and say which reading you chose. Delete
scratch scripts you made to check your work. Before you write your report, check each claim in it
against a command result or a file you saw in this session: report only what you can point to, and
say plainly what isn't verified."""


def worker_prompt(task: str, repo: Path, verify_cmds: list[str], hint: str = "",
                  advisor: bool = False) -> str:
    v = "\n".join(f"  {c}" for c in verify_cmds)
    return f"""{task}

Working directory: {repo}. Read the nearest AGENTS.md before editing.
Done means these commands pass (run them yourself before finishing):
{v}
Do not commit.

{FINISH}

{VERIFY}

{SCOPE}{hint}{CONSULT if advisor else ""}"""


PR_SET_PDEATHSIG = 1


def die_with(parent: int) -> None:
    """Stop when the orchestrator that started this task stops, SIGKILL included, and take what this
    task started along: a resume would start a second worker on the same task. The orchestrator
    starts this process as the leader of its own group, so the group holds only this task (a jailed
    command dies with bwrap). Only a leader signals its group: never someone else's."""
    def stop(*_) -> None:
        if os.getpgrp() == os.getpid():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(0, signal.SIGTERM)
            time.sleep(1)
            os.killpg(0, signal.SIGKILL)
        os._exit(143)

    signal.signal(signal.SIGTERM, stop)
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
    except (ImportError, OSError, AttributeError) as e:
        log(f"warning: this task can't follow its orchestrator's death: {e}")
    if os.getppid() != parent:  # it was gone before prctl
        stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", help="the task prompt, or - to read it from stdin (it has no size limit there)")
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
    ap.add_argument("--done-ids", nargs="*", default=[],
                    help="definition-of-done ids ([D1].., [U1]..) the report's done_check must show "
                         "met with evidence; a gap counts as a failed attempt")
    ap.add_argument("--max-model", choices=models.MODEL_ORDER,
                    help="never route or escalate above this model (e.g. opus: no Fable)")
    ap.add_argument("--advisor", choices=["sonnet", "opus", "fable"],
                    help="let the worker consult this model at decision points (claude --advisor)")
    ap.add_argument("--parent", type=int, help=argparse.SUPPRESS)  # set by the orchestrator: die with it
    ap.add_argument("--jail", choices=jail.MODES, default="auto",
                    help="confine the worker and verify commands with bubblewrap (default: where it works)")
    ap.add_argument("--trusted-verify", action="append", default=[], metavar="CMD",
                    help="a verify command of the harness's own, run outside the jail")
    ap.add_argument("--start", metavar="MODEL/EFFORT",
                    help="start config before learned floors (default: the worker role, opus/medium)")
    ap.add_argument("--dry-run", action="store_true", help="only print the routing decision")
    a = ap.parse_args(argv)
    if a.parent:
        die_with(a.parent)
    if a.task == "-":
        a.task = sys.stdin.buffer.read().decode("utf-8", "replace")
    repo = a.repo.expanduser().resolve()
    router.validate_type(a.type)
    # the worker is told to run the verify commands itself, so it must be allowed to
    a.allowed_tools += a.extra_tools + [f"Bash({c})" for c in a.verify]
    task_id = uuid.uuid4().hex[:10]
    report_schema = json.loads(a.report_schema) if a.report_schema else None
    extra = ["--exclude-dynamic-system-prompt-sections"] if a.exclude_dynamic_prompt else []

    decision = router.route(a.type, a.tier, max_model=a.max_model, start=a.start)
    log(f"task {task_id} {a.type} {a.tier} -> {decision['model']}/{decision['effort']} ({decision['source']})")
    if a.dry_run:
        print(json.dumps(decision))
        return 0

    try:
        confine = jail.confinement("required" if a.sandbox else a.jail, router.DIR)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 3
    if a.jail != "off" and not confine:
        log(f"warning: the worker is NOT confined: {jail.available()}")
    if not a.no_baseline:
        ok, out = verify(a.baseline or a.verify, repo, a.verify_timeout, confine, a.trusted_verify)
        if not ok:
            log("baseline check FAILS before any change -> environment problem; not starting.")
            print(out[-3000:], file=sys.stderr)
            return 3
    if git(repo, "status", "--porcelain").strip():
        log("warning: repo has uncommitted changes; consider running in a worktree")

    settings = {"permissions": {"blockWritesOutsideWorkingDirectories": True}}
    if a.sandbox:
        # strict: otherwise a command leaves the sandbox with dangerouslyDisableSandbox
        settings["sandbox"] = {"enabled": True, "autoAllowBashIfSandboxed": True,
                               "allowUnsandboxedCommands": False, "failIfUnavailable": True}
    rung, probe, hint = decision["rung"], decision["probe"], ""
    summary = {"task_id": task_id, "type": a.type, "tier": a.tier, "steps": [], "proposals": []}
    advisor_off = None  # set when a launch failed because of the advisor: run on without it
    escalations, spent = 0, 0.0

    def remaining():
        return None if a.budget_usd is None else a.budget_usd - spent

    while True:
        model, effort = router.cfg(rung)
        session, worker, vout, ok = None, {}, "", False
        cost = 0.0
        metrics: dict = {}
        for attempt in range(1, a.attempts + 1):
            adv = [] if advisor_off else models.advisor_args(model, a.advisor, a.max_model)
            prompt = worker_prompt(a.task, repo, a.verify, hint, bool(adv)) if attempt == 1 else (
                "Verification still fails. Fix it, then end with your full JSON report again."
                + (CONSULT_RETRY if adv else "") + " Output:\n" + vout[-5000:])
            log(f"attempt {attempt}/{a.attempts} at {model}/{effort}"
                + (f" (advisor {adv[1]})" if adv else ""))
            kw = dict(perm=a.permission_mode, tools=a.allowed_tools, resume=session,
                      timeout=a.claude_timeout, settings=settings, schema=report_schema, confine=confine)
            worker = claude(prompt, model, effort, repo, budget_usd=remaining(),
                            extra_args=extra + models.fallback_args(model) + adv, **kw)
            if adv and worker.get("error_kind") in ("exec", "unparseable") \
                    and not worker.get("total_cost_usd") \
                    and models.ADVISOR_LAUNCH_FAIL.search(str(worker.get("result"))):
                # The launch failed on the advisor (e.g. Fable usage-credits consent), before the
                # model did anything: not evidence about the model. Run again without it.
                advisor_off = f"{adv[1]} advisor refused at launch: {str(worker.get('result'))[-300:]}"
                summary["advisor_off"] = advisor_off
                log(f"advisor off for this task: {advisor_off}")
                if attempt == 1:
                    prompt = worker_prompt(a.task, repo, a.verify, hint, False)
                worker = claude(prompt, model, effort, repo, budget_usd=remaining(),
                                extra_args=extra + models.fallback_args(model), **kw)
                adv = []
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
            ok, vout = verify(a.verify, repo, a.verify_timeout, confine, a.trusted_verify)
            if ok and a.done_ids:
                gaps = done_gaps(worker.get("structured"), a.done_ids)
                if gaps:
                    ok = False
                    vout = ("The verify commands pass, but your definition of done is not met yet. "
                            "Finish these items, then report a done_check entry with evidence for "
                            "every id:\n" + "\n".join(f"- {g}" for g in gaps))
                    log(f"done check: {len(gaps)} gap(s): {'; '.join(gaps)[:400]}")
            if ok:
                break
        step = {"config": f"{model}/{effort}", "probe": probe, "ok": ok, "cost_usd": round(cost, 6),
                "metrics": metrics,
                "advisor": (models.advisor_args(model, a.advisor, a.max_model) or [None, None])[1]
                if not advisor_off else None}
        if models.fell_back(model, worker.get("model_id")):
            # Fable unavailable -> Claude Code ran the fallback. Not evidence about either model.
            step["fell_back_to"] = worker.get("model_id")
            log(f"{model} unavailable; ran on {worker.get('model_id')}")
        summary["steps"].append(step)
        evidence = "fell_back_to" not in step

        if ok:
            if evidence:
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

        if worker.get("error_kind") == "refusal":
            # A safety classifier declined the request: not the model's capability, and a
            # stronger model of the same family would decline too. Stop and say why.
            cat = worker.get("refusal_category") or "uncategorised"
            step.update(cause="refusal", evidence=f"declined by a safety classifier ({cat})")
            summary["result"], summary["refusal_category"] = "refused", cat
            log(f"refused at {model}/{effort} ({cat}); not routing evidence")
            break

        c = classify(a.task, rung, worker, vout, repo, a.claude_timeout, a.verify)
        spent += c.get("classifier_cost") or 0
        step.update(cause=c["cause"], evidence=c.get("evidence"),
                    classifier_cost_usd=c.get("classifier_cost"))
        log(f"FAIL at {model}/{effort}: {c['cause']} - {c.get('evidence')}")
        if evidence:
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
            new = router.next_rung(rung, "effort" if c["cause"] == "effort" else "capability",
                                   a.max_model)
            if probe:  # a failed probe goes straight back to the learned floor
                new = decision["rung"] + 1
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
    return {"ok": 0, "environment": 3, "budget": 7, "refused": 9}.get(summary["result"], 4)


if __name__ == "__main__":
    sys.exit(main())
