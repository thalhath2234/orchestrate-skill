"""orca_mode.py: run an orchestrate job with Orca workers, coordinated by the agent that started it.

In headless mode one detached process runs every subagent itself (`claude -p` through
run_task.py). In Orca mode the parent agent is the Orca coordinator: this module never waits.
Each call does one step and returns, and Orca wakes the parent when a worker reports.

  start      prepare the job (setup, baseline, advisor plan check), create an Orca Run, start a
             worker for every ready task (up to the parallel cap), return
  worker_done
             a worker reported: check its worktree (scope, verify, done criteria). Pass: commit,
             merge into orchestrate/<job>, release the worker. Fail: send the failure back to the
             same worker (2 attempts per model), then classify and start a stronger model in the
             same worktree, as run_task.py does. Then start whatever became ready; when every task
             has merged, run the review round (reviewer, cleanup, advisor's final check)
  worker_lost
             a worker stopped without reporting: counts as a failed attempt
  resume     gates and manager answers, then the same step

The script keeps the brain: briefs, routing, checks, merges, the manager, review and the advisor
run here, exactly as in headless mode. Orca only runs the workers and carries their mail.

Each worker gets its own child worktree, branched from the integration branch, named
<job>--<task>. It writes its JSON report to .orchestrate/report.json and sends worker_done with
--report-path. Cost of Orca workers is not visible to the script, so budget_usd covers only the
script's own model calls (manager, reviewer, advisor, classifier) in this mode.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import jail
import models
import orchestrate as O
import router
import run_task as RT

WAITING = 10          # exit code: workers are running; the coordinator ends its turn
ATTEMPTS = 2          # tries per model config before classifying, as in run_task.py
REPORT_REL = ".orchestrate/report.json"


class OrcaError(RuntimeError):
    pass


# ------------------------------------------------------------------ the CLI ---
def orca_bin() -> str:
    """The Orca CLI for this session, resolved as the orchestration skill says: never a bare
    `orca` on Linux outside Orca, where that is the GNOME screen reader."""
    for var in ("ORCHESTRATE_ORCA_BIN", "ORCA_CLI_COMMAND"):
        if os.environ.get(var):
            return os.environ[var]
    if os.environ.get("ORCA_DEV_REPO_ROOT"):
        return "orca-dev"
    return "orca-ide" if sys.platform.startswith("linux") else "orca"


def in_orca() -> bool:
    """True when this process runs inside an Orca agent session (or a test fakes one)."""
    return bool(os.environ.get("ORCA_AGENT_SESSION_ID") or os.environ.get("ORCHESTRATE_ORCA_BIN"))


def orca(*args: str, timeout: int = 300) -> dict:
    p = subprocess.run([orca_bin(), *args, "--json"], capture_output=True, text=True, timeout=timeout)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        out = None
    if p.returncode != 0 or not isinstance(out, dict) or out.get("ok") is False:
        raise OrcaError(f"`orca {' '.join(args[:2])}` failed (exit {p.returncode}): "
                        f"{(p.stdout + p.stderr).strip()[-1500:]}")
    return out.get("result", out)


def try_orca(*args: str) -> dict | None:
    try:
        return orca(*args)
    except (OrcaError, subprocess.TimeoutExpired, OSError) as e:
        O.log(f"orca: {e}")
        return None


@contextlib.contextmanager
def step_lock(job: O.Job):
    """One step at a time per job: two worker_done wakes can arrive close together."""
    job.dir.mkdir(parents=True, exist_ok=True)
    with open(job.dir / ".orca-step.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ----------------------------------------------------------------- workers ---
def orca_state(job: O.Job) -> dict:
    return job.state.setdefault("orca", {"run": None, "dispatches": {}})


def ensure_run(job: O.Job, plan: dict) -> str:
    st = orca_state(job)
    if not st.get("run"):
        r = orca("orchestration", "run-create", "--objective", f"orchestrate job {job.id}: {plan['goal']}"[:400])
        st["run"] = r.get("id") or (r.get("run") or {}).get("id") or r.get("runId")
        if not st["run"]:
            raise OrcaError(f"run-create returned no run id: {json.dumps(r)[:500]}")
        job.save()
    return st["run"]


def exclude_reports(repo: Path) -> None:
    """Keep each worker's report out of git: one line in the shared info/exclude."""
    common = Path(O.git(repo, "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else repo / common
    f = common / "info" / "exclude"
    f.parent.mkdir(parents=True, exist_ok=True)
    text = O.read_text(f) or ""
    if "/.orchestrate/" not in text.splitlines():
        f.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "/.orchestrate/\n")


def _common_dir(path: Path) -> Path | None:
    p = subprocess.run([*jail.GIT, "rev-parse", "--git-common-dir"], cwd=path, capture_output=True, text=True)
    if p.returncode:
        return None
    d = Path(p.stdout.strip())
    return (d if d.is_absolute() else path / d).resolve()


def placement(job: O.Job, plan: dict) -> list[str]:
    """Where a fresh worker's worktree goes. new-child nests it under the coordinator's own Orca
    workspace, which is right when the coordinator works in the job's repo; for any other repo,
    new-top-level in that repo (it must be added to Orca). plan.orca_worktree overrides."""
    repo = Path(job.state["repo"])
    mode = plan.get("orca_worktree")
    if not mode:
        mode = "new-child" if _common_dir(Path.cwd()) == _common_dir(repo) else "new-top-level"
    return ["--worktree", mode] + (["--repo", f"path:{repo}"] if mode == "new-top-level" else [])


def spec_for(job: O.Job, plan: dict, task: dict, hint: str = "") -> str:
    """The brief plus how to work and how to report, for an Orca worker."""
    integ = Path(job.state["integration_wt"])
    upstream = {d: job.state["tasks"].get(d, {}) for d in task.get("depends_on", [])}
    brief = O.build_brief(plan, task, integ, upstream)
    setup = plan.get("setup") or []
    v = "\n".join(f"  {c}" for c in task["verify"])
    parts = [brief, "", "## How to work",
             "Your working directory is an Orca worktree made for this task, branched from the "
             "job's integration branch. Work only there.",
             *(["Before anything else, run these setup commands:", *[f"  {c}" for c in setup]] if setup else []),
             f"Done means these commands pass (run them yourself before finishing):\n{v}",
             "Do not commit: the orchestrator commits and merges your work.",
             "", RT.FINISH, "", RT.VERIFY, "", RT.SCOPE, "",
             "## When you finish",
             f"1. Write your JSON report (the format above) to `{REPORT_REL}` in your working "
             "directory; create the folder if needed. It stays out of git.",
             "2. Send worker_done with --outcome succeeded when you believe the task is done, or "
             "--outcome failed when you could not finish it, and pass --report-path with the "
             "absolute path of that file.",
             "The orchestrator then runs the verify commands and checks every done criterion "
             "itself. If something fails, you'll get a message with the output: fix it, write the "
             "report again and send worker_done again."]
    if hint:
        parts += ["", "## Note from the orchestrator", hint]
    return "\n".join(parts)


def start_worker(job: O.Job, plan: dict, tid: str, rung: int, *, fresh: bool, hint: str = "",
                 probe: bool = False, attempt: int = 1) -> str | None:
    """Start an Orca worker for `tid` at ladder rung `rung`. fresh: a new child worktree from the
    integration head; otherwise the task's existing worktree (an escalation keeps the work).
    Returns an error message, or None."""
    task = next(t for t in plan["tasks"] if t["id"] == tid)
    st = job.state["tasks"][tid]
    o = st.setdefault("orca", {"starts": 0, "escalations": 0})
    model, effort = router.cfg(rung)
    spec = spec_for(job, plan, task, hint)
    spec_file = job.dir / "briefs" / f"{tid}.md"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(spec)
    args = ["orchestration", "worker-start", "--spec-file", str(spec_file),
            "--task-title", f"{job.id}/{tid}: {task['title']}"[:120], "--run", ensure_run(job, plan),
            "--agent", plan.get("orca_agent", "claude"),
            "--model", models.MODEL_IDS[model], "--effort", effort, "--full"]
    integ = Path(job.state["integration_wt"])
    if fresh:
        o["starts"] += 1
        name = f"{job.id}--{tid}" + (f"-{o['starts']}" if o["starts"] > 1 else "")
        start_sha = O.git(integ, "rev-parse", "HEAD")
        args += [*placement(job, plan), "--name", name, "--base-branch", job.state["branch"],
                 "--setup", "skip"]
    else:
        args += ["--worktree", f"path:{o['worktree']}"]
    try:
        r = orca(*args, timeout=600)
    except (OrcaError, subprocess.TimeoutExpired) as e:
        return str(e)
    did = r.get("dispatchId")
    if not did:
        return f"worker-start returned no dispatch id: {json.dumps(r)[:500]}"
    if fresh:
        wt = next((e["id"].split("::", 1)[-1] for e in r.get("effects") or []
                   if e.get("kind") == "worktree" and e.get("id")), None)
        if not wt or not Path(wt).is_dir():
            return f"worker-start created no worktree I can find: {json.dumps(r.get('effects'))[:500]}"
        o.update(worktree=wt, branch=O.git(Path(wt), "branch", "--show-current"), start_sha=start_sha)
        st["start_sha"] = start_sha
    o.update(dispatch=did, rung=rung, attempt=attempt, probe=probe, task_id=r.get("taskId"))
    orca_state(job)["dispatches"][did] = tid
    st.update(status="running", started=st.get("started") or time.time())
    job.save()
    O.log(f"start {tid} [{task['type']} {task['tier']}] as Orca worker {did} at {model}/{effort}")
    O.task_log(job, tid, f"started Orca worker {did} at {model}/{effort} (attempt {attempt}) in "
                         f"{o.get('worktree')}; the worker itself runs in an Orca terminal. Brief: {spec_file}"
               + (f"\nhint: {hint[:3000]}" if hint else ""))
    return None


def release(did: str | None) -> None:
    if did:
        try_orca("orchestration", "worker-release", "--dispatch", did)


def remove_worktree(path: str | None) -> None:
    """After a merge: Orca removes the worktree and deletes the branch only if it can prove it merged."""
    if path and Path(path).exists():
        try_orca("worktree", "rm", "--worktree", f"path:{path}", "--force", "--delete-branch")


def worker_tail(did: str | None, limit: int = 40) -> str:
    if not did:
        return ""
    r = try_orca("orchestration", "worker-read", "--dispatch", did, "--limit", str(limit))
    return json.dumps(r)[-6000:] if r else ""


# ------------------------------------------------------------------- steps ---
def start(job: O.Job, plan: dict, approve: list[str], max_parallel: int | None,
          skip_baseline: bool, answer: str | None = None) -> int:
    with step_lock(job):
        job.state["mode"] = "orca"
        job.state.pop("pid", None)
        exclude_reports(Path(job.state["repo"]))
        code = O.prepare_job(job, plan, approve, max_parallel, skip_baseline, answer)
        if code is not None:
            return code
        return pump(job, plan)


def stopped(job: O.Job, plan: dict) -> int | None:
    """A job that already ended (failed, done, aborted) takes no more steps."""
    st = job.state.get("status")
    if st in O.TERMINAL:
        O.log(f"job {job.id} already ended ({st}); this worker's report changes nothing")
        return O.EXIT.get(st, 1)
    return None


def worker_done(job: O.Job, plan: dict, payload: dict) -> int:
    with step_lock(job):
        if (code := stopped(job, plan)) is not None:
            return code
        did = payload.get("dispatchId") or payload.get("dispatch_id")
        tid = orca_state(job)["dispatches"].get(did)
        st = job.state["tasks"].get(tid or "", {})
        if not tid or st.get("status") != "running" or st.get("orca", {}).get("dispatch") != did:
            O.log(f"worker_done from {did}: not a running task of this job; nothing to do")
            return pump(job, plan)
        if O.guard_snapshot() != job.state.get("guard"):
            st.update(status="failed", error="modified the orchestrate skill files")
            job.save()
            return O.finish(job, plan, "failed",
                            f"SECURITY: the orchestrate skill files in {O.HERE.parent} changed while "
                            f"{tid} ran. Job stopped. Inspect them before resuming.")
        check(job, plan, tid, payload)
        return pump(job, plan)


def worker_lost(job: O.Job, plan: dict, did: str, reason: str) -> int:
    with step_lock(job):
        if (code := stopped(job, plan)) is not None:
            return code
        tid = orca_state(job)["dispatches"].get(did)
        st = job.state["tasks"].get(tid or "", {})
        if not tid or st.get("status") != "running" or st.get("orca", {}).get("dispatch") != did:
            O.log(f"{did}: not a running task of this job; nothing to do")
            return pump(job, plan)
        fail_attempt(job, plan, tid, f"the worker stopped without reporting: {reason}", lost=True)
        return pump(job, plan)


def resume(job: O.Job, plan: dict, approve: list[str], max_parallel: int | None,
           skip_baseline: bool, answer: str | None = None) -> int:
    return start(job, plan, approve, max_parallel, skip_baseline, answer)


# ------------------------------------------------------------- the checks ---
def read_report(wt: Path, payload: dict) -> dict | None:
    p = Path(payload.get("reportPath") or wt / REPORT_REL)
    if not p.is_absolute():
        p = wt / p
    try:
        p.resolve().relative_to(wt.resolve())
    except ValueError:
        return None  # a report outside the worker's own worktree is not its report
    r = O.extract_json(O.read_text(p) or "")
    return r if isinstance(r, dict) and "summary" in r else None


def check(job: O.Job, plan: dict, tid: str, payload: dict) -> None:
    """The checks run_task.py runs after an attempt, here on the worker's worktree."""
    task = next(t for t in plan["tasks"] if t["id"] == tid)
    st = job.state["tasks"][tid]
    o = st["orca"]
    wt = Path(o["worktree"])
    report = read_report(wt, payload)
    ok, out = True, ""
    bad = O.scope_violations(wt, task["owns"], base=o.get("start_sha"))
    if bad:
        bad, granted = O.claim_unowned(job.id, tid, bad)
    if bad:
        ok, out = False, ("OUT OF SCOPE: you changed files that another task owns, or shared hotspots "
                          "or migrations. Revert these and report the need under open_issues instead:\n"
                          + "\n".join(f"  {b}" for b in bad) + "\nYou own: " + ", ".join(task["owns"]))
    if ok:
        ok, out = RT.verify(O.task_checks(plan, task), wt, 1800, O.confinement(plan))
    if ok and report is None:
        ok, out = False, (f"The verify commands pass, but there is no readable report at {wt / REPORT_REL}. "
                          "Write the JSON report there, then send worker_done again.")
    if ok:
        gaps = RT.done_gaps(report, O.done_ids(plan, task))
        if gaps:
            ok, out = False, ("The verify commands pass, but your definition of done is not met yet. "
                              "Finish these items, then report a done_check entry with evidence for "
                              "every id:\n" + "\n".join(f"- {g}" for g in gaps))
    model, effort = router.cfg(o["rung"])
    step = {"config": f"{model}/{effort}", "attempt": o["attempt"], "ok": ok, "dispatch": o["dispatch"],
            "outcome": payload.get("outcome"), "probe": o.get("probe", False)}
    st.setdefault("steps", []).append(step)
    st["report"] = report
    O.task_log(job, tid, f"worker_done from {o['dispatch']} ({payload.get('outcome') or 'no outcome'}): "
                         f"checks {'passed' if ok else 'FAILED'} at {model}/{effort}, attempt {o['attempt']}"
               + (f"\n{out[-6000:]}" if out else "")
               + (f"\nreport summary: {report.get('summary')}" if report else ""))
    if not ok:
        fail_attempt(job, plan, tid, out, report=report, step=step)
        return
    res = {"wt": str(wt), "branch": o["branch"], "report": report}
    try:
        merged, info = O.commit_and_merge(job, plan, task, res, remove_worktree=False)
    except O.UsageLimit:  # the integrator's: the merge was undone and the task still waits for this step
        raise
    except Exception as e:  # noqa: BLE001
        merged, info = False, f"commit/merge error: {e}"
    router.record(task_id=f"{job.id}/{tid}", task_type=task["type"], tier=task["tier"], rung=o["rung"],
                  ok=True, cause=None, model_id=models.MODEL_IDS[model], probe=o.get("probe", False))
    st.update(ended=time.time(), result="ok" if merged else "merge_failed")
    O.task_log(job, tid, ("merged into the integration branch" if merged else f"merge FAILED: {info}"))
    if not merged:
        st.update(status="failed", error=info)
        O.log(f"FAIL  {tid}: {info}")
        queue_event(job, task_failed_event(job, plan, tid))
        return
    st.update(status="done", files=json.loads(info))
    O.log(f"done  {tid} ({len(st['files'])} files merged)")
    release(o["dispatch"])
    remove_worktree(o["worktree"])
    O.raise_bar(job, plan, (report or {}).get("raise_bar") or [], f"subagent:{tid}")
    issues = (report or {}).get("open_issues") or []
    if issues:
        rest, auto = O.triage_issues(job, plan, tid, issues)
        if auto:
            O._log_decision(job, {"t": time.time(), "event": "open_issues", "task": tid, "auto": True,
                                  "note": "amended the pending owners directly (no manager call)",
                                  "actions": auto}, {"applied": auto})
        if rest:
            queue_event(job, {"kind": "open_issues", "task": tid, "open_issues": rest,
                              "report": report, "files": st["files"]})
    job.save()


def fail_attempt(job: O.Job, plan: dict, tid: str, out: str, *, report: dict | None = None,
                 step: dict | None = None, lost: bool = False) -> None:
    """A failed attempt: the same worker tries again, then the failure is classified and the
    ladder decides, exactly as in run_task.py."""
    task = next(t for t in plan["tasks"] if t["id"] == tid)
    st = job.state["tasks"][tid]
    o = st["orca"]
    if o["attempt"] < ATTEMPTS:
        o["attempt"] += 1
        if lost:  # the worker is gone: a new one in the same worktree, same model, next attempt
            err = start_worker(job, plan, tid, o["rung"], fresh=False, hint=out,
                               probe=o.get("probe", False), attempt=o["attempt"])
            if not err:
                return
            out = f"{out}\nRestarting the worker failed: {err}"
        else:
            sent = try_orca("orchestration", "send", "--to", f"dispatch:{o['dispatch']}",
                            "--run", orca_state(job)["run"], "--subject", f"{job.id}/{tid}: checks failed",
                            "--body", "Verification still fails. Fix it, write the report again and send "
                            "worker_done again. Output:\n" + out[-5000:])
            if sent is not None:
                O.log(f"retry {tid}: sent the failure back to {o['dispatch']} (attempt {o['attempt']})")
                O.task_log(job, tid, f"sent the failure back to {o['dispatch']} (attempt {o['attempt']})")
                job.save()
                return
            out = f"{out}\nSending the failure back to the worker failed."
    model, effort = router.cfg(o["rung"])
    final = ((report or {}).get("summary") or "") + "\n" + worker_tail(o.get("dispatch"))
    c = RT.classify(spec_for(job, plan, task), o["rung"], {"result": final}, out, Path(o["worktree"]),
                    1800, task["verify"])
    if c["cause"] == "usage_limit":  # the classifier's own call: no evidence about the worker's model
        c = {**c, "cause": "environment"}
    if c.get("classifier_cost"):
        O.record_call(job, "classifier", {"total_cost_usd": c["classifier_cost"]}, tid,
                      "/".join(RT.classifier_cfg()))
    if step is not None:
        step.update(cause=c["cause"], evidence=c.get("evidence"))
    else:
        st.setdefault("steps", []).append({"config": f"{model}/{effort}", "attempt": o["attempt"], "ok": False,
                                           "dispatch": o.get("dispatch"), "cause": c["cause"],
                                           "evidence": c.get("evidence")})
    st.setdefault("proposals", []).extend(router.record(
        task_id=f"{job.id}/{tid}", task_type=task["type"], tier=task["tier"], rung=o["rung"], ok=False,
        cause=c["cause"], model_id=models.MODEL_IDS[model], probe=o.get("probe", False),
        note=c.get("evidence", "")))
    O.log(f"FAIL  {tid} at {model}/{effort}: {c['cause']} - {c.get('evidence')}")
    O.task_log(job, tid, f"FAIL at {model}/{effort}: {c['cause']} - {c.get('evidence')}")
    if c["cause"] == "environment":
        return give_up(job, plan, tid, "environment", out)
    hint = ""
    if c["cause"] == "context":
        note = (c.get("agents_md") or {}).get("text") or c.get("evidence", "")
        if c.get("agents_md"):
            st["proposals"].append(router.propose_context(
                task["type"], str(Path(job.state["repo"]) / c["agents_md"]["file"]), note, c.get("evidence", "")))
        new, hint = o["rung"], f"Note from a previous failed attempt: {note}"
    else:
        new = router.next_rung(o["rung"], "effort" if c["cause"] == "effort" else "capability",
                               plan.get("max_model"))
        if o.get("probe"):  # a failed probe goes straight back to the learned floor
            new = o["rung"] + 1
        if new != o["rung"]:
            hint = ("A previous attempt by a weaker configuration left changes in this worktree; "
                    "review them, keep what is right, revert what is wrong.")
    o["escalations"] = o.get("escalations", 0) + 1
    if o["escalations"] > int(plan.get("max_escalations", 2)) or (new == o["rung"] and c["cause"] != "context"):
        return give_up(job, plan, tid, "gave_up", out)
    release(o.get("dispatch"))
    hint = f"{hint}\n\nThe last check failed with:\n{out[-3000:]}".strip()
    err = start_worker(job, plan, tid, new, fresh=False, hint=hint)
    if err:
        give_up(job, plan, tid, "gave_up", f"{out}\nStarting the next worker failed: {err}")


def give_up(job: O.Job, plan: dict, tid: str, result: str, out: str) -> None:
    st = job.state["tasks"][tid]
    st.update(status="failed", result=result, ended=time.time(), error=out[-2000:])
    O.log(f"FAIL  {tid}: {result}")
    O.task_log(job, tid, f"gave up ({result}); the worker stays open for you to look at\n{out[-3000:]}")
    queue_event(job, task_failed_event(job, plan, tid))
    job.save()  # the worker stays open so you can look at its worktree and chat


def task_failed_event(job: O.Job, plan: dict, tid: str) -> dict:
    st = job.state["tasks"][tid]
    return {"kind": "task_failed", "task": tid, "task_def": next(t for t in plan["tasks"] if t["id"] == tid),
            "result": st.get("result"), "error": st.get("error"),
            "steps": st.get("steps"), "report": st.get("report"),
            "log_tail": worker_tail((st.get("orca") or {}).get("dispatch"))}


def queue_event(job: O.Job, event: dict) -> None:
    if O.manager_cfg(job.plan)["enabled"]:
        job.state.setdefault("pending_events", []).append(event)
    job.save()


# -------------------------------------------------------------- scheduling ---
def pump(job: O.Job, plan: dict) -> int:
    """Settle manager events, start what is ready, and review when everything has merged.
    Returns WAITING while workers run, or the job's exit code."""
    plan = job.plan  # actions and raised criteria are written to the job's plan.json
    while True:
        if job.state.get("human_question"):
            return hold_for_answer(job, plan)
        queue = job.state.pop("pending_events", [])
        if queue:
            ev = queue[0] if len(queue) == 1 else {"kind": "batch", "events": queue}
            r = O.manage(job, plan, ev)  # synchronous: nothing else can happen until it decides
            plan = job.plan
            if r.get("hint") == "ask_human":
                job.state["human_question"] = r.get("question")
                return hold_for_answer(job, plan)
            if r.get("hint") == "abort":
                for t, st in job.state["tasks"].items():  # stop what still runs: nothing may merge now
                    if st.get("status") == "running":
                        try_orca("orchestration", "worker-stop", "--dispatch", st["orca"]["dispatch"])
                        st["status"] = "stopped"
                return O.finish(job, plan, "failed", f"manager aborted the job: {r.get('reason')}")
            continue
        O.mark_blocked(job, plan)
        T = {t["id"]: t for t in plan["tasks"]}
        for tid in T:  # tasks the manager added or split since the last step
            job.state["tasks"].setdefault(tid, {"status": "pending", "title": T[tid]["title"],
                                                "type": T[tid]["type"], "tier": T[tid]["tier"]})
        sts = job.state["tasks"]
        running = [t for t in T if sts[t]["status"] == "running"]
        cap = (job.state.get("capacity") or {}).get("cap") or 1
        budget = plan.get("budget_usd")
        for tid in T:
            st = sts[tid]
            if st["status"] not in ("pending", "awaiting_approval") or len(running) >= cap:
                continue
            if not all(sts.get(d, {}).get("status") in ("done", "skipped") for d in T[tid].get("depends_on", [])):
                continue
            if T[tid].get("gate") and tid not in job.state.get("approved", []):
                if st["status"] != "awaiting_approval":
                    O.log(f"{tid} needs approval: {T[tid]['gate']}")
                st["status"] = "awaiting_approval"
                continue
            if budget and job.state.get("cost_usd", 0) >= budget:
                break
            d = router.route(T[tid]["type"], T[tid]["tier"], max_model=plan.get("max_model"),
                             start="/".join(models.role("worker", plan)))
            err = start_worker(job, plan, tid, d["rung"], fresh=True, probe=d["probe"])
            if err:
                st.update(status="failed", result="start_failed", error=err)
                O.log(f"FAIL  {tid}: could not start an Orca worker: {err}")
                queue_event(job, task_failed_event(job, plan, tid))
                break  # let the manager see it before anything else starts
            running.append(tid)
        job.save()
        if job.state.get("pending_events"):
            continue
        if running:
            names = ", ".join(f"{t} ({sts[t]['orca']['dispatch']})" for t in running)
            job.state.update(status="running", message=f"waiting on Orca workers: {names}")
            job.save()
            print(f"WAITING on {len(running)} Orca worker(s): {names}.\nEnd your turn. When Orca wakes you "
                  f"with a worker_done, run: orchestrate.py worker-done {job.id} --dispatch <dispatchId> "
                  f"--report-path <reportPath> (both from its Payload)")
            return WAITING
        statuses = {t: sts[t]["status"] for t in T}
        if any(s in ("failed", "blocked", "stopped") for s in statuses.values()):
            bad = {t: s for t, s in statuses.items() if s in ("failed", "blocked", "stopped")}
            env = [t for t in bad if sts[t].get("result") == "environment"]
            return O.finish(job, plan, "environment" if env and len(env) == len(
                [t for t in bad if bad[t] == "failed"]) else "failed",
                f"tasks failed/blocked: {bad}. Their Orca workers stay open for you to look at. Fix and "
                f"`orchestrate.py resume {job.id}` (done tasks are kept).")
        if any(s == "awaiting_approval" for s in statuses.values()):
            waiting = [t for t, s in statuses.items() if s == "awaiting_approval"]
            return O.finish(job, plan, "awaiting_approval",
                            f"waiting for approval: {waiting}. Resume with: orchestrate.py resume {job.id} "
                            + " ".join(f"--approve {w}" for w in waiting))
        if any(s == "pending" for s in statuses.values()):
            return O.finish(job, plan, "budget", f"budget ${budget} reached")
        r = O.review_round(job, plan)
        if isinstance(r, int):
            return r
        plan = job.plan
        if r == "fix_round":
            add_fix_task(job, plan, job.state["review"].pop("owed_fix"))
            plan = job.plan


def hold_for_answer(job: O.Job, plan: dict) -> int:
    """The manager asked the user something. Until the answer comes (resume --answer), nothing
    new starts and no review runs; workers already running finish, and their work is checked and
    merged as it reports. Queued events wait for the answer too."""
    running = [t for t, st in job.state["tasks"].items() if st.get("status") == "running"]
    q = job.state["human_question"]
    tail = (f"\n{len(running)} worker(s) are still running ({', '.join(running)}); when Orca wakes you with "
            f"their worker_done, run worker-done as usual. Nothing new starts until the answer."
            if running else "")
    return O.finish(job, plan, "awaiting_human",
                    f"the manager needs your input: {q}\nAnswer with: orchestrate.py resume {job.id} "
                    f"--answer \"...\"{tail}")


def add_fix_task(job: O.Job, plan: dict, owed: dict) -> None:
    """Orca mode's fix round: the review findings become one task, run by an Orca worker like any
    other, after every other task (so its wide ownership overlaps nothing that can still run)."""
    rnd, findings = owed["round"], owed["findings"]
    owns = sorted({p for t in plan["tasks"] for p in t["owns"]} | set(plan.get("review_may_edit", [])))
    tid = f"review-fix-{rnd}"
    criteria = [f"Fixed, with a test where it is behaviour: {str(f.get('issue', ''))[:300]}"
                + (f" ({f['file']})" if f.get("file") else "") for f in findings[:8]]
    while len(criteria) < O.MIN_DONE["T2"]:
        criteria.append("The job's verify commands pass after the fixes")
    plan["tasks"].append({
        "id": tid, "title": f"Review fixes, round {rnd}", "type": f"bugfix:{plan.get('area', 'cross')}",
        "tier": "T2", "goal": "Fix these review findings on the job's merged work:\n"
                              + json.dumps(findings, indent=2)[:8000],
        "owns": owns, "depends_on": [t["id"] for t in plan["tasks"]], "done": criteria,
        "verify": list(plan["verify"])})
    job.state["tasks"][tid] = {"status": "pending", "title": f"Review fixes, round {rnd}",
                               "type": plan["tasks"][-1]["type"], "tier": "T2", "added_by": "review"}
    O.write_json(job.dir / "plan.json", plan)
    job.save()
    O.log(f"review round {rnd}: {len(findings)} blocking finding(s); queued {tid} for an Orca worker")
