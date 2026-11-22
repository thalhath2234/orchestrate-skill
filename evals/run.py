#!/usr/bin/env python3
"""Snapshot evals for orchestrate's model judgments (P0.4).

Each case builds a state directly (a fixture repo, a plan, job state, an event or a diff), runs
the REAL prompt builder and parser (orchestrate.ask_manager / orchestrate.review /
run_task.classify) against the real `claude`, and grades the outcome, not the path.

  python3 evals/run.py                         # every case, 1 trial
  python3 evals/run.py --core                  # the CI set: cases marked "core"
  python3 evals/run.py --role manager --trials 3
  python3 evals/run.py --role manager --config opus/medium     # sweep one config (P3.4)
  python3 evals/run.py --role classifier --config haiku/low --config sonnet/low
  python3 evals/run.py --json out.json --min-pass 0.9          # gate: exit 1 below 90%

Costs real money: each manager/reviewer case is one Opus call per trial; advisor cases are one
or two Fable calls, planner cases one Fable planning session, worker cases one Opus coding session,
UI reviewer cases one Opus call that reads the fixture screenshots, and parent cases one or two
Opus turns of the real orchestrate skill (its scripts are stubbed, so no job runs). Cases live in
evals/cases/<role>/<id>.json; see evals/README.md for the format. State is written to a
scratch ORCHESTRATE_DIR, never to your real one.

A trial whose model call was served by a fallback model (no Fable access: Opus answered) is
INVALID: it is left out of the pass rate, and a config where every trial fell back fails
--min-pass. --json also records the git commit, the claude version and the date.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import copy
import datetime
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "orchestrate" / "scripts"
CASES = Path(__file__).resolve().parent / "cases"
sys.path.insert(0, str(SCRIPTS))


def make_repo(root: Path, base: dict, head: dict | None = None, dirty: dict | None = None) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir(parents=True)

    def write(files):
        for p, c in files.items():
            f = repo / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(c)
    g = ["git", "-c", "user.email=e@e", "-c", "user.name=eval"]
    write({"AGENTS.md": "# rules\n", **base})
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([*g, "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    if head:
        write(head)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run([*g, "commit", "-qm", "head"], cwd=repo, check=True)
    if dirty:
        write(dirty)
    return repo, base_sha


class StubJob:
    def __init__(self, d: Path, state: dict):
        self.id, self.dir, self.state = "eval", d, state
        d.mkdir(parents=True, exist_ok=True)

    def save(self):
        pass


def full_plan(case: dict, repo: Path) -> dict:
    plan = {"job": "eval", "goal": "eval goal", "verify": ["make test"], **copy.deepcopy(case.get("plan", {}))}
    plan["repo"] = str(repo)
    return plan


def job_state(case: dict, plan: dict, repo: Path, base_sha: str) -> dict:
    tasks = {t["id"]: {"status": "pending", "title": t["title"], "type": t["type"], "tier": t["tier"]}
             for t in plan.get("tasks", [])}
    for tid, st in (case.get("state") or {}).items():
        tasks.setdefault(tid, {}).update(st)
    return {"job": "eval", "repo": str(repo), "base": "main", "base_sha": base_sha, "branch": "orchestrate/eval",
            "integration_wt": str(repo), "status": "running", "approved": [], "cost_usd": 0.0,
            "review": {"rounds": []}, "tasks": tasks}


def served(pairs) -> dict:
    """`pairs`: (requested model, model id the CLI reported) per call. A call answered by a
    fallback model (fable requested, opus served) makes the trial INVALID, not a pass or fail."""
    import models
    pairs = [(str(m or ""), i) for m, i in pairs]
    return {"served": sorted({i for _, i in pairs if i}),
            "fell_back": any(models.fell_back(m, i) for m, i in pairs)}


def _calls(job) -> dict:
    return served((str(c.get("config") or "").split("/")[0], c.get("model_id"))
                  for c in job.state.get("calls") or [])


# ------------------------------------------------------------------ roles ---
def _touches(acts: list, tid: str) -> bool:
    """An action on task `tid`: its `task` is that id, or a task object with that id. Never a
    substring: an add_task whose new task owns apps/api/... does not touch task "api"."""
    return any(a.get("task") == tid or (isinstance(a.get("task"), dict) and a["task"].get("id") == tid)
               for a in acts if isinstance(a, dict))


def grade_manager(exp: dict, decision: dict, applied: dict) -> list[str]:
    """The manager's batch against `expect`. `mentions` searches the actions only (their
    instructions, briefs and questions), never the free-text note: naming a file in the note
    while the retry leaves it out is the mistake the scope cases exist to catch."""
    acts = decision.get("actions") or []
    kinds = [a.get("action") for a in acts if isinstance(a, dict)] or ["none"]
    fails = []
    if exp.get("valid", True) and applied.get("errors"):
        fails.append(f"invalid actions: {applied['errors']}")
    if exp.get("any_action") and not set(kinds) & set(exp["any_action"]):
        fails.append(f"expected one of {exp['any_action']}, got {kinds}")
    if exp.get("no_action") and set(kinds) & set(exp["no_action"]):
        fails.append(f"must not {sorted(set(kinds) & set(exp['no_action']))}")
    for tid in exp.get("touches", []):
        if not _touches(acts, tid):
            fails.append(f"expected an action on {tid}")
    text = json.dumps(acts).lower()
    for needle in exp.get("mentions", []):
        if needle.lower() not in text:
            fails.append(f"no action mentions {needle!r}")
    return fails


def run_manager(o, case: dict, tmp: Path, config: str | None) -> dict:
    repo, sha = make_repo(tmp, case.get("files", {}))
    plan = full_plan(case, repo)
    if config:
        m, e = config.split("/")
        plan["manager"] = {**plan.get("manager", {}), "model": m, "effort": e}
    job = StubJob(tmp / "job", job_state(case, plan, repo, sha))
    (job.dir / "plan.json").write_text(json.dumps(plan))
    decision = o.ask_manager(job, plan, case["event"])
    applied = o.apply_actions(job, copy.deepcopy(plan), decision, case["event"])
    corrected = None
    if applied.get("errors"):  # as in production (manage): one corrective re-ask with the errors
        corrected = applied["errors"]
        retry = {"kind": "rejected_actions", "errors": applied["errors"],
                 "previous_event": case["event"].get("kind"), "task": case["event"].get("task")}
        decision = o.ask_manager(job, plan, retry)
        applied = o.apply_actions(job, copy.deepcopy(plan), decision, case["event"])
    fails = grade_manager(case["expect"], decision, applied)
    kinds = [a.get("action") for a in decision.get("actions") or [] if isinstance(a, dict)]
    label = ",".join(sorted(set(k for k in kinds if k != "none")) or ["none"])
    return {"ok": not fails, "label": label, "fails": fails, "cost": job.state["cost_usd"],
            "detail": decision.get("note"), "corrected": corrected, **_calls(job)}


SEV_RANK = {"minor": 1, "major": 2, "blocker": 3}


def _says(f: dict, term: str) -> bool:
    """`term`, or one of its `|` alternatives, in the finding's text."""
    text = json.dumps(f).lower()
    return any(alt.lower() in text for alt in term.split("|"))


def grade_reviewer(exp: dict, v: dict) -> list[str]:
    """The reviewer's verdict against `expect`. Only the reviewer's own blocker/major findings that
    it stands by count: not the harness's (an unassessed criterion quotes the plan, so it would name
    what the reviewer never found), and not one it marked low confidence. `finding_mentions` and
    `finding_file` must all hold in one finding."""
    fails = ["reviewer produced no verdict"] if v.get("reviewer_error") else []
    serious = [f for f in v.get("findings", []) if isinstance(f, dict) and SEV_RANK.get(f.get("severity"), 0) >= 2
               and f.get("confidence") != "low" and f.get("source") != "harness"]
    if exp.get("verdict") and v.get("verdict") != exp["verdict"]:
        fails.append(f"verdict {v.get('verdict')} != {exp['verdict']}")
    want, where = exp.get("finding_mentions", []), exp.get("finding_file")
    if (want or where) and not any((not where or where in str(f.get("file", ""))) and all(_says(f, n) for n in want)
                                   for f in serious):
        fails.append(f"no blocker/major finding of its own" + (f" on {where}" if where else "")
                     + (f" mentions {want}" if want else ""))
    hit = [n for n in exp.get("no_finding_mentions", []) if any(_says(f, n) for f in serious)]
    if hit:
        fails.append(f"a blocker/major finding mentions {hit} (re-argues a settled decision)")
    return fails


def run_reviewer(o, case: dict, tmp: Path, config: str | None) -> dict:
    case = _from(case)
    repo, sha = make_repo(tmp, case.get("base_files", {}), case.get("head_files", {}))
    plan = full_plan(case, repo)
    if config:
        m, e = config.split("/")
        plan["reviewer"] = {"model": m, "effort": e}
    st = job_state(case, plan, repo, sha)
    for tid, rep in (case.get("reports") or {}).items():
        st["tasks"].setdefault(tid, {"status": "done", "title": tid})["report"] = rep
    job = StubJob(tmp / "job", st)
    v = o.review(job, plan, case.get("verify_output", "$ make test\nok"))
    fails = grade_reviewer(case["expect"], v)
    return {"ok": not fails, "label": v.get("verdict"), "fails": fails, "cost": job.state["cost_usd"],
            "detail": v.get("summary"), **_calls(job)}


def run_classifier(rt, case: dict, tmp: Path, config: str | None) -> dict:
    repo, _ = make_repo(tmp, case.get("files", {}), dirty=case.get("diff_files", {}))
    if config:
        os.environ["ROUTER_CLASSIFIER"] = config
    worker = {"result": case.get("final_message", ""), **case.get("worker", {})}
    out = rt.classify(case["task"], case.get("rung", 2), worker, case.get("verify_output", ""), repo, 600,
                      case.get("verify", ["make test"]))
    exp = case["expect"]
    fails = [] if out["cause"] == exp["cause"] else [f"cause {out['cause']} != {exp['cause']}"]
    return {"ok": not fails, "label": out["cause"], "fails": fails, "cost": out.get("classifier_cost") or 0,
            "detail": out.get("evidence")}


def _from(case: dict) -> dict:
    """`"from": "<role>/<id>"` reuses another case's fixture (files, plan, reports) and overrides it."""
    if not case.get("from"):
        return case
    base = json.loads((CASES / f"{case['from']}.json").read_text())
    merged = {**base, **{k: v for k, v in case.items() if k not in ("from", "plan_patch")}}
    if case.get("plan_patch"):
        merged["plan"] = _patch(copy.deepcopy(base.get("plan", {})), case["plan_patch"])
    return merged


def _patch(plan: dict, patch: dict) -> dict:
    """plan_patch: top-level keys replace; "tasks": {id: {field: value}} edits tasks in place."""
    for k, v in patch.items():
        if k == "tasks" and isinstance(v, dict):
            for t in plan.get("tasks", []):
                t.update(v.get(t["id"], {}))
        else:
            plan[k] = v
    return plan


def run_advisor(o, case: dict, tmp: Path, config: str | None) -> dict:
    case = _from(case)
    stage = case.get("stage", "plan")
    if stage == "final":
        repo, sha = make_repo(tmp, case.get("base_files", {}), case.get("head_files", {}))
    else:
        repo, sha = make_repo(tmp, case.get("files") or case.get("base_files", {}))
    plan = full_plan(case, repo)
    if config:
        plan.setdefault("models", {})["advisor"] = config
    spent, asked = [], []
    body = (o.final_body(repo, sha, plan, case.get("verify_output", "$ make test\nok"))
            if stage == "final" else o.plan_body(plan, plan.get("baseline", plan["verify"])
                                                  if case.get("baseline_passed", True) else None))
    a = o.advise(stage, plan, repo, body, on_call=lambda res, cfg: (
        spent.append(res.get("total_cost_usd") or 0), asked.append((cfg.split("/")[0], res.get("model_id")))))
    exp = case["expect"]
    fails = []
    if a is None:
        return {"ok": False, "label": "error", "fails": ["advisor produced no usable advice"], "cost": sum(spent),
                **served(asked)}
    blocking = o.advice_blocking(a)
    label = "revise" if blocking else "proceed"
    if exp.get("verdict") and label != exp["verdict"]:
        fails.append(f"{label} != {exp['verdict']} ({a.get('summary', '')[:200]})")
    if exp.get("finding_mentions"):
        text = json.dumps(blocking + (a.get("raise_bar") or [])).lower()
        miss = [n for n in exp["finding_mentions"] if n.lower() not in text]
        if miss:
            fails.append(f"no blocker/major finding or raised criterion mentions {miss}")
    return {"ok": not fails, "label": label, "fails": fails, "cost": sum(spent), **served(asked),
            "detail": f"[{a.get('config')}{' escalated' if a.get('escalated') else ''}] {a.get('summary')}"
                      + "".join(f" | BLOCKS ({f.get('severity')}/{f.get('confidence')}): {f.get('issue')}" for f in blocking)}


def grade_planner(exp: dict, case: dict, plan: dict, errs: list, owned_by) -> list[str]:
    """The planner's plan against `expect`. `owned_by(path, pattern)` is orchestrate.owned.
    `assumption_matches` / `no_assumption_matches` are regexes (case-insensitive) over the
    plan's `assumptions`: choices the planner made that the user hasn't confirmed. `decisions`
    is the exact list the plan's `decisions` must hold (only the user's, verbatim)."""
    fails = [f"invalid plan: {errs}"] if errs else []
    if plan.get("definition_of_done") != case["done"]:
        fails.append("definition_of_done not verbatim")
    owned = [p for t in plan.get("tasks", []) for p in t.get("owns", [])]
    for f in exp.get("owned", []):
        if not any(owned_by(f, pat) for pat in owned):
            fails.append(f"no task owns {f}")
    text = json.dumps({k: plan.get(k) for k in ("acceptance", "tasks")}).lower()
    for n in exp.get("criteria_mention", []):
        if n.lower() not in text:
            fails.append(f"no criterion mentions {n!r}")
    if len(plan.get("acceptance") or []) < exp.get("min_acceptance", 1):
        fails.append(f"only {len(plan.get('acceptance') or [])} acceptance criteria")
    n = len(plan.get("tasks") or [])
    if not exp.get("min_tasks", 1) <= n <= exp.get("max_tasks", 99):
        fails.append(f"{n} tasks, expected {exp.get('min_tasks', 1)}-{exp.get('max_tasks', 99)}")
    assumed = [a for a in plan.get("assumptions") or [] if isinstance(a, str)]
    for rx in exp.get("assumption_matches", []):
        if not any(re.search(rx, a, re.I) for a in assumed):
            fails.append(f"no assumption matches {rx!r} (assumptions: {assumed})")
    for rx in exp.get("no_assumption_matches", []):
        hit = [a for a in assumed if re.search(rx, a, re.I)]
        if hit:
            fails.append(f"assumption {hit[0]!r} matches {rx!r}, which the user already settled")
    if "decisions" in exp and (plan.get("decisions") or []) != exp["decisions"]:
        fails.append(f"decisions are {plan.get('decisions')!r}, expected {exp['decisions']!r}")
    return fails


@contextlib.contextmanager
def recording_calls(o, asked: list):
    """Record (requested model, served model id) for every orchestrate.claude call made inside.
    draft_plan returns no model ids, and a planner answered by the fallback model is INVALID."""
    real = o.claude

    def rec(prompt, model, *args, **kw):
        res = real(prompt, model, *args, **kw)
        asked.append((model, res.get("model_id")))
        return res
    o.claude = rec
    try:
        yield asked
    finally:
        o.claude = real


def run_planner(o, case: dict, tmp: Path, config: str | None) -> dict:
    case = _from(case)
    repo, _ = make_repo(tmp, case.get("files") or case.get("base_files", {}))
    asked = []
    with recording_calls(o, asked):
        plan, errs, cost, raw = o.draft_plan(repo, case["goal"], case["done"], config,
                                             acceptance=case.get("acceptance"), decisions=case.get("decisions"),
                                             non_goals=case.get("non_goals"))
    if plan is None:
        return {"ok": False, "label": "error", "fails": [f"no plan: {raw[-300:]}"], "cost": cost, **served(asked)}
    fails = grade_planner(case["expect"], case, plan, errs, o.owned)
    n = len(plan.get("tasks") or [])
    return {"ok": not fails, "label": f"{n} tasks", "fails": fails, "cost": cost, **served(asked),
            "detail": "; ".join(t.get("id", "?") for t in plan.get("tasks", []))
                      + "".join(f" | ASSUMED: {a}" for a in plan.get("assumptions") or [])}


def run_worker(o, case: dict, tmp: Path, config: str | None) -> dict:
    """One task through the real brief and run_task.py, on a tiny repo; graded on the repo and report."""
    repo, sha = make_repo(tmp, case["files"])
    plan = full_plan(case, repo)
    task = plan["tasks"][0]
    brief = o.build_brief(plan, task, repo, {})
    cmd = [sys.executable, str(SCRIPTS / "run_task.py"), "--repo", str(repo), "--type", task["type"],
           "--tier", task["tier"], "--report-schema", json.dumps(o.schemas.REPORT),
           "--start", config or "/".join(o.models.role("worker", plan)), "--max-escalations", "0",
           "--attempts", "1", "--no-baseline", "--done-ids", *o.done_ids(plan, task)]
    for v in task["verify"]:
        cmd += ["--verify", v]
    cmd.append(brief)
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=3600)
    summary = o.extract_json(p.stdout) or {}
    report = summary.get("report") or {}
    cost = sum((x.get("cost_usd") or 0) + (x.get("classifier_cost_usd") or 0) for x in summary.get("steps", []))
    exp = case["expect"]
    fails = []
    if exp.get("ok", True) and p.returncode != 0:
        fails.append(f"task did not pass verify + done check: {summary.get('result')}")
    for f, needle in (exp.get("file_contains") or {}).items():
        if needle not in ((repo / f).read_text() if (repo / f).exists() else ""):
            fails.append(f"{f} lacks {needle!r}")
    for f, needle in (exp.get("file_lacks") or {}).items():
        if (repo / f).exists() and needle in (repo / f).read_text():
            fails.append(f"{f} still contains {needle!r} (out of scope change expected to be left)")
    for f in exp.get("unchanged", []):
        d = subprocess.run(["git", "diff", "--quiet", sha, "--", f], cwd=repo).returncode
        if d:
            fails.append(f"{f} changed, but it is out of scope")
    if exp.get("issue_mentions"):
        text = json.dumps(report.get("open_issues") or []).lower()
        miss = [n for n in exp["issue_mentions"] if n.lower() not in text]
        if miss:
            fails.append(f"open_issues never mention {miss}")
    steps = summary.get("steps") or []
    return {"ok": not fails, "label": "ok" if p.returncode == 0 else "failed", "fails": fails, "cost": cost,
            "detail": (report.get("summary") or summary.get("result") or "")[:200],
            "served": sorted({s["fell_back_to"] for s in steps if s.get("fell_back_to")}),
            "fell_back": any("fell_back_to" in s for s in steps)}


UI_FIXTURES = CASES / "ui_reviewer" / "fixtures"
SHOT_DEFAULTS = {"step": None, "status": 200, "title": "", "console": [], "page_errors": [], "failed_requests": [],
                 "step_errors": [], "notes": [], "tiles": [], "tasks": []}


def grade_ui_reviewer(exp: dict, v: dict, ui_tag: str, reply: dict | None = None) -> list[str]:
    """The review step's verdict against `expect`. Only blocker/major findings that carry the UI
    tag (`ui_tag`, orchestrate.UI_TAG) count: a code finding is not the UI reviewer's. `expected`
    ([shot, kind] pairs) and `no_expected` (shots) are checked against the UI reviewer's own
    reply (`reply`): its `expected` entries are what waive the harness's evidence findings."""
    fails = [f"no UI verdict: {v.get('summary')}"] if v.get("reviewer_error") else []
    waived = [e for e in (reply or {}).get("expected") or []
              if isinstance(e, dict) and str(e.get("why") or "").strip()]

    def names(e: dict, shot: str) -> bool:  # "share--desktop" names round-1/share--desktop(.png), not my-share--desktop
        return re.search(rf"(?:^|/){re.escape(shot)}(?:\.png)?$", str(e.get("shot", "")).strip()) is not None

    for shot, kind in exp.get("expected", []):
        if not any(names(e, shot) and e.get("kind") == kind for e in waived):
            fails.append(f"the UI reviewer did not list {shot} {kind} as expected (expected: {waived})")
    for shot in exp.get("no_expected", []):
        hit = [e for e in waived if names(e, shot)]
        if hit:
            fails.append(f"the UI reviewer waved {shot} through as expected: {hit[0]}")
    serious = [f for f in v.get("findings", []) if str(f.get("issue", "")).startswith(ui_tag)
               and SEV_RANK.get(f.get("severity"), 0) >= 2]
    if exp.get("verdict") and v.get("verdict") != exp["verdict"]:
        fails.append(f"verdict {v.get('verdict')} != {exp['verdict']}")
    text = json.dumps(serious).lower()
    miss = [n for n in exp.get("finding_mentions", []) if n.lower() not in text]
    if miss:
        fails.append(f"no blocker/major UI finding mentions {miss}")
    hit = [n for n in exp.get("no_finding_mentions", []) if n.lower() in text]
    if hit:
        fails.append(f"a blocker/major UI finding mentions {hit}")
    if exp.get("finding_shot"):
        on = [f for f in serious if exp["finding_shot"] in f"{f.get('issue', '')} {f.get('evidence', '')}"]
        if not on:
            fails.append(f"no blocker/major UI finding names shot {exp['finding_shot']}")
        elif exp.get("finding_severity") and not any(f.get("severity") == exp["finding_severity"] for f in on):
            fails.append(f"the findings on {exp['finding_shot']} are not {exp['finding_severity']}")
    return fails


def run_ui_reviewer(o, case: dict, tmp: Path, config: str | None) -> dict:
    """The UI review step after a passing code review: the real prompt builder, re-ask and
    enforcement (ui_judge_step) on fixture screenshots. Graded on the UI findings and verdict."""
    case = _from(case)
    repo, sha = make_repo(tmp, case.get("base_files", {}), case.get("head_files", {}))
    plan = full_plan(case, repo)
    plan.setdefault("ui", {"dev": "npm run dev", "url": "http://127.0.0.1:5173"})
    if config:
        plan.setdefault("models", {})["ui_reviewer"] = config
    st = job_state(case, plan, repo, sha)
    root = tmp / "screenshots"
    shots = []
    for s in case["shots"]:
        sid = f"round-1/{s['name']}--{s['viewport']}"
        (root / "round-1").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(UI_FIXTURES / s["png"], root / f"{sid}.png")
        shots.append({**SHOT_DEFAULTS, "id": sid, "file": f"{sid}.png",
                      **{k: v for k, v in s.items() if k != "png"}})
    ui = {"ran": True, "skipped": None, "error": None, "app_failed": False, "url": plan["ui"]["url"],
          "dir": str(root / "round-1"), "shots": shots, "page_failures": [], "restored": [], "verdict": None}
    st.update(screenshots_root=str(root), review={"rounds": [{"ui": ui}]})
    job = StubJob(tmp / "job", st)
    v = o.ui_judge_step(job, plan, ui, {"verdict": "pass", "findings": [], "summary": "code review: pass"})
    fails = grade_ui_reviewer(case["expect"], v, o.UI_TAG, ui.get("verdict"))
    return {"ok": not fails, "label": v.get("verdict"), "fails": fails, "cost": job.state["cost_usd"],
            "detail": v.get("summary"), **_calls(job)}


# --------------------------------------------------------------- parent ---
# The parent role runs the real skills (skills/orchestrate*/SKILL.md) as a plugin copy in one or
# two `claude -p` turns. Its scripts are replaced by STUB_PY, which records each call and replays
# the case's canned output, so no job runs and no subagent costs money.
PARENT_DEFAULT = "opus/medium"
PARENT_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Skill", "Agent", "Task", "TodoWrite"]
PARENT_EXPECT = {"files_unchanged", "no_in_place_edit", "bash_includes", "bash_lacks", "writes_lack", "calls_include", "calls_lack", "calls_before",
                 "reply_includes", "reply_matches", "reply_lacks", "said_includes", "said_matches", "said_lacks"}
# Keys that only pass when the turn did something. Every parent turn needs one: a turn graded only
# on what it must not do passes when the skill never loaded or the session errored out.
PARENT_POSITIVE = {"bash_includes", "calls_include", "calls_before", "reply_includes", "reply_matches",
                   "said_includes", "said_matches"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Session links to the calling Claude Code or Orca, which would change what the skill does.
PARENT_ENV_DROP = ("ORCA_", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                   "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_MESSAGING_", "CLAUDE_PID")
STUB_PY = '''#!/usr/bin/env python3
"""orchestrate eval stub: records the call and replays the case's canned output. No job runs."""
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
name = os.path.basename(__file__)[:-3]
args = sys.argv[1:]
with open(os.path.join(here, "_calls.jsonl"), "a") as f:
    f.write(json.dumps([name] + args) + "\\n")
with open(os.path.join(here, "_stub.json")) as f:
    stub = json.load(f)
sub = name if name == "run_task" else next((a for a in args if not a.startswith("-")), "")
r = stub.get(sub) or {"exit": 0, "stdout": "{}"}
sys.stdout.write(r.get("stdout", ""))
sys.stderr.write(r.get("stderr", ""))
sys.exit(int(r.get("exit", 0)))
'''
REFUSE_SH = ('#!/bin/sh\necho \'{"is_error": true, "result": "orchestrate evals: model calls from scripts are '
             'off inside a parent eval"}\'\nexit 1\n')


def plugin_copy(dst: Path, stub: dict) -> tuple[Path, str]:
    """A copy of this repo's plugin under a unique name, with orchestrate.py and run_task.py
    swapped for STUB_PY and the parent skill's `S=` pinned to the copy's scripts (otherwise the
    `ls -d` lookup finds an installed orchestrate and runs it for real)."""
    name = f"orcheval{uuid.uuid4().hex[:8]}"
    for d in (".claude-plugin", "hooks", "skills"):
        shutil.copytree(ROOT / d, dst / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (dst / ".claude-plugin" / "marketplace.json").unlink()
    pj = dst / ".claude-plugin" / "plugin.json"
    pj.write_text(json.dumps({**json.loads(pj.read_text()), "name": name}, indent=2))
    scripts = dst / "skills" / "orchestrate" / "scripts"
    for s in ("orchestrate.py", "run_task.py"):
        (scripts / s).write_text(STUB_PY)
        (scripts / s).chmod(0o755)
    (scripts / "_stub.json").write_text(json.dumps(stub))
    md = dst / "skills" / "orchestrate" / "SKILL.md"
    text, n = S_BLOCK.subn(lambda m: f"```bash\nS={shlex.quote(str(scripts))}\n```", md.read_text(), count=1)
    if n != 1:
        raise RuntimeError("skills/orchestrate/SKILL.md has no ```bash block that sets S=: update plugin_copy "
                           "in evals/run.py")
    md.write_text(text)
    return scripts, name


# The parent skill's first ```bash block with a line that starts `S=`: where it finds its scripts.
S_BLOCK = re.compile(r"^```bash\n(?:(?!^```).)*?^S=(?:(?!^```).)*?^```$", re.S | re.M)


def parse_stream(stdout: str) -> dict:
    """One `claude -p --output-format stream-json` turn: Bash commands, written paths, the final
    reply, the session id, cost and the models that served it."""
    out = {"bash": [], "writes": [], "tools": [], "reply": "", "texts": [], "session_id": None, "cost": 0.0,
           "model_usage": {}, "is_error": False, "subtype": None}
    for line in stdout.split("\n"):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        out["session_id"] = ev.get("session_id") or out["session_id"]
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    out["texts"].append(c.get("text") or "")
                elif c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    out["tools"].append(c.get("name"))
                    if c.get("name") == "Bash":
                        out["bash"].append(str(inp.get("command", "")))
                    elif c.get("name") in WRITE_TOOLS:
                        out["writes"].append(str(inp.get("file_path") or inp.get("notebook_path") or ""))
        elif ev.get("type") == "result":
            out["reply"] = str(ev.get("result") or "")
            out["cost"] = ev.get("total_cost_usd") or 0.0
            out["model_usage"] = ev.get("modelUsage") or {}
            out["is_error"] = bool(ev.get("is_error"))
            out["subtype"] = ev.get("subtype")
    if not out["reply"] and out["texts"]:
        out["reply"] = out["texts"][-1]
    return out


IN_PLACE = {"sed": ("-i", "--in-place"), "gsed": ("-i", "--in-place"), "perl": ("-i",), "ruby": ("-i",)}
PREFIXES = {"env", "sudo", "command", "exec", "nohup", "nice", "xargs", "time", "builtin"}


def in_place_edits(cmd: str) -> list[str]:
    """The simple commands in `cmd` that edit a file in place (sed -i in any spelling, perl -pi...).
    Each is parsed like a shell would, so a quoted `|` or `;` in a sed script, or a `-i` that
    belongs to the next command, can't confuse it."""
    import shlex
    found = []
    for line in str(cmd).splitlines():
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            words = list(lex)
        except ValueError:  # unbalanced quotes: judge the line as one command
            words = line.split()
        simple: list[list[str]] = [[]]
        for w in words:
            if w and set(w) <= set(";&|()"):
                simple.append([])
            else:
                simple[-1].append(w)
        for argv in simple:
            i = 0
            while i < len(argv) and (re.match(r"^[A-Za-z_]\w*=", argv[i]) or argv[i] in PREFIXES or
                                     (argv[i] == "timeout" and i + 1 < len(argv))):
                i += 2 if argv[i] == "timeout" else 1
                while i < len(argv) and argv[i].startswith("-") and argv[i - 1] in ("env", "sudo", "nice"):
                    i += 1
            if i >= len(argv):
                continue
            flags = IN_PLACE.get(argv[i].rsplit("/", 1)[-1])
            if flags and any(a.startswith("--in-place") or (a.startswith("-") and not a.startswith("--")
                                                             and "i" in a[1:].split(".", 1)[0] and "-i" in flags
                                                             and re.match(r"^-[A-Za-z]+", a))
                             for a in argv[i + 1:]):
                found.append(" ".join(argv[i:]))
    return found


def grade_parent(exp: dict, turn: dict, calls: list[list[str]]) -> list[str]:
    """One turn against its `expect`. Tool calls first: Bash commands (attempts count, even one
    the approval hook then stops), written paths and the stub's call log (`orchestrate wait x`,
    `run_task ...`). Then narrow checks on the final reply (`reply_*`) or on everything the turn
    said (`said_*`: every text block, the final reply included). `*_includes` is literal."""
    fails = []
    said = [" ".join(c) for c in calls]
    spoken = "\n".join([*turn.get("texts", []), turn["reply"]])
    for rx in exp.get("bash_includes", []):
        if not any(re.search(rx, b) for b in turn["bash"]):
            fails.append(f"no Bash command matches {rx!r}")
    for rx in exp.get("bash_lacks", []):
        hit = [b for b in turn["bash"] if re.search(rx, b)]
        if hit:
            fails.append(f"ran {hit[0][:160]!r} (matches {rx!r})")
    if exp.get("no_in_place_edit"):
        hit = [e for b in turn["bash"] for e in in_place_edits(b)]
        if hit:
            fails.append(f"edited in place: {hit[0][:160]!r}")
    if exp.get("files_unchanged"):
        fails += [f"changed {f}, which this turn may not edit" for f in turn.get("changed") or []]
    for rx in exp.get("writes_lack", []):
        hit = [w for w in turn["writes"] if re.search(rx, w)]
        if hit:
            fails.append(f"wrote {hit[0]} (matches {rx!r})")
    for rx in exp.get("calls_include", []):
        if not any(re.search(rx, c) for c in said):
            fails.append(f"no script call matches {rx!r} (calls: {said[:6]})")
    for rx in exp.get("calls_lack", []):
        hit = [c for c in said if re.search(rx, c)]
        if hit:
            fails.append(f"called {hit[0][:160]!r} (matches {rx!r})")
    for first, then in exp.get("calls_before", []):
        at = next((i for i, c in enumerate(said) if re.search(then, c)), None)
        if at is not None and not any(re.search(first, c) for c in said[:at]):
            fails.append(f"called {said[at][:160]!r} (matches {then!r}) before any call matching {first!r}")
    for s in exp.get("reply_includes", []):
        if s not in turn["reply"]:
            fails.append(f"reply lacks {s[:80]!r}")
    for rx in exp.get("reply_matches", []):
        if not re.search(rx, turn["reply"], re.I | re.M):
            fails.append(f"reply does not match {rx!r}")
    for rx in exp.get("reply_lacks", []):
        if re.search(rx, turn["reply"], re.I | re.M):
            fails.append(f"reply matches {rx!r}")
    for s in exp.get("said_includes", []):
        if s not in spoken:
            fails.append(f"never said {s[:80]!r}")
    for rx in exp.get("said_matches", []):
        if not re.search(rx, spoken, re.I | re.M):
            fails.append(f"nothing it said matches {rx!r}")
    for rx in exp.get("said_lacks", []):
        if re.search(rx, spoken, re.I | re.M):
            fails.append(f"said something that matches {rx!r}")
    return fails


def parent_env(state: Path, refuse: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PARENT_ENV_DROP)}
    # Belt and braces: a real orchestrate reached by another path can't spend or touch real state.
    return {**env, "ROUTER_CLAUDE_BIN": str(refuse), "ORCHESTRATE_DIR": str(state), "ORCHESTRATE_DASHBOARD": "0"}


def run_parent(o, case: dict, tmp: Path, config: str | None) -> dict:
    import claude_cli
    import models
    repo, _ = make_repo(tmp, case.get("files", {}))
    scripts, name = plugin_copy(tmp / "plugin", case.get("stub") or {})
    refuse = tmp / "refuse-claude"
    refuse.write_text(REFUSE_SH)
    refuse.chmod(0o755)
    env = parent_env(tmp / "state", refuse)
    m, e = (config or case.get("model") or PARENT_DEFAULT).split("/")
    log = scripts / "_calls.jsonl"
    fails, cost, session, pairs, labels, out = [], 0.0, None, [], [], {"reply": ""}
    for i, turn in enumerate(case["turns"][:2], 1):
        text = turn["user"] if session or not case.get("invoke", True) else f"/{name}:orchestrate {turn['user']}"
        cmd = [claude_cli.claude_bin(), "-p", text, "--plugin-dir", str(tmp / "plugin"),
               "--setting-sources", "project,local", "--output-format", "stream-json", "--verbose",
               "--model", m, "--effort", e, "--permission-mode", "dontAsk", "--allowedTools", *PARENT_TOOLS,
               "--max-budget-usd", f"{float(case.get('budget_usd', 3)):.2f}", *models.fallback_args(m)]
        if session:
            cmd += ["--resume", session]
        before = len(log.read_text().splitlines()) if log.exists() else 0
        p = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=1200)
        out = parse_stream(p.stdout)
        calls = [json.loads(x) for x in (log.read_text().splitlines()[before:] if log.exists() else [])]
        cost += out["cost"] or 0
        usage = out["model_usage"]
        pairs.append((m, next((k for k in usage if m in k), next(iter(usage), None))))
        # A session that errored (the skill never loaded, the budget ran out) is a failed turn,
        # whatever it replied: a turn graded only on what it must not do would pass it.
        if not out["session_id"] or p.returncode or out["is_error"]:
            why = f"exit {p.returncode}" + (f", is_error ({out['subtype']})" if out["is_error"] else "")
            fails.append(f"turn {i}: claude failed ({why}): {(out['reply'] or p.stderr or p.stdout)[-300:]}")
            break
        session = out["session_id"]
        out["changed"] = [f for f, body in (case.get("files") or {}).items()
                          if not (repo / f).is_file() or (repo / f).read_text(errors="replace") != body]
        fails += [f"turn {i}: {f}" for f in grade_parent(turn.get("expect") or {}, out, calls)]
        labels.append(",".join(sorted({c[0] if c[0] == "run_task" else " ".join(c[:2]) for c in calls})) or "no calls")
    return {"ok": not fails, "label": " | ".join(labels) or "error", "fails": fails, "cost": round(cost, 4),
            "detail": out["reply"][:300], **served(pairs)}


RUNNERS = {"manager": run_manager, "reviewer": run_reviewer, "classifier": run_classifier,
           "advisor": run_advisor, "planner": run_planner, "worker": run_worker,
           "ui_reviewer": run_ui_reviewer, "parent": run_parent}


def load_cases(roles, ids, core) -> list[tuple[str, dict]]:
    out = []
    for role in roles:
        for f in sorted((CASES / role).glob("*.json")):
            c = json.loads(f.read_text())
            c.setdefault("id", f.stem)
            if ids and not any(i in c["id"] for i in ids):
                continue
            if core and not c.get("core"):
                continue
            out.append((role, c))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", action="append", choices=list(RUNNERS))
    ap.add_argument("--case", action="append", help="substring of case ids to run")
    ap.add_argument("--core", action="store_true", help="only cases marked core (the CI set)")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--config", action="append", help="model/effort to sweep, e.g. opus/medium (repeatable)")
    ap.add_argument("--json", help="write full results here")
    ap.add_argument("--min-pass", type=float, default=0.0, help="exit 1 if the pass rate is below this")
    ap.add_argument("--min-role-pass", type=float, default=ROLE_FLOOR,
                    help="with --min-pass: also exit 1 if any one role's pass rate is below this "
                         f"(default {ROLE_FLOOR}), so a whole role can't fail behind a good overall mean")
    ap.add_argument("--keep", action="store_true", help="keep the scratch fixture repos for inspection")
    ap.add_argument("--no-hermetic", action="store_true",
                    help="load your own hooks, and your MCP servers in calls that may have them (workers; judgment "
                         "roles never do) (default: neither loads)")
    a = ap.parse_args(argv)
    # Hermetic by default: the user's MCP servers and hooks change what a prompt does, so a run
    # measured with them doesn't compare with one measured without (claude_cli.call reads this).
    if a.no_hermetic:
        os.environ.pop("ORCHESTRATE_HERMETIC", None)
    else:
        os.environ["ORCHESTRATE_HERMETIC"] = "1"

    scratch = Path(tempfile.mkdtemp(prefix="orch-eval-"))
    os.environ["ORCHESTRATE_DIR"] = str(scratch / "state")
    import router  # noqa: F401  (reads ORCHESTRATE_DIR at import)
    importlib.reload(router)
    import orchestrate
    import run_task
    o = importlib.reload(orchestrate)
    rt = importlib.reload(run_task)

    cases = load_cases(a.role or list(RUNNERS), a.case, a.core)
    if not cases:
        print("no cases matched")
        return 2
    meta = {**run_meta(exclude=[a.json] if a.json else []), "hermetic": not a.no_hermetic}
    print("run: " + "  ".join(f"{k} {v}" for k, v in meta.items()), flush=True)
    results = []
    for config in a.config or [None]:
        for role, case in cases:
            outs = []
            for trial in range(a.trials):
                tmp = scratch / f"{role}-{case['id']}-{config or 'default'}-{trial}".replace("/", "_")
                t0 = time.time()
                try:
                    r = RUNNERS[role](rt if role == "classifier" else o, case, tmp, config)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "label": "error", "fails": [repr(e)], "cost": 0}
                r["secs"] = round(time.time() - t0, 1)
                outs.append(r)
            valid = [r for r in outs if not r.get("fell_back")]
            labels = collections.Counter(r["label"] for r in valid)
            row = {"role": role, "case": case["id"], "config": config or "default", "core": bool(case.get("core")),
                   "invalid": not valid, "fell_back": len(outs) - len(valid),
                   "pass_rate": sum(r["ok"] for r in valid) / len(valid) if valid else None,
                   "consistency": labels.most_common(1)[0][1] / len(valid) if valid else None,
                   "cost_usd": round(sum(r["cost"] for r in outs), 4), "trials": outs}
            results.append(row)
            print(row_line(row, outs), flush=True)
    by = collections.defaultdict(list)
    for r in results:
        by[(r["role"], r["config"])].append(r)
    print()
    for (role, config), rows in sorted(by.items()):
        ok = [r for r in rows if not r["invalid"]]
        fell = sum(r["fell_back"] for r in rows)
        rate = (f"pass {sum(r['pass_rate'] for r in ok) / len(ok):.2f}  consistency "
                f"{sum(r['consistency'] for r in ok) / len(ok):.2f}") if ok else "pass -     consistency -   "
        print(f"{role:10} {config:13} {rate}  cost ${sum(r['cost_usd'] for r in rows):.2f} "
              f"({len(rows)} cases x {a.trials} trials)" + (f"  {fell} trial(s) fell back, not counted" if fell else ""))
    dead = fallback_configs(results)
    for config, ids in dead.items():
        print(f"WARNING: every trial of {config} was served by {', '.join(ids) or 'a fallback model'}: the "
              "requested model is unavailable on this account, so these rows measured the fallback; not counted")
    valid_rows = [r for r in results if not r["invalid"]]
    overall = sum(r["pass_rate"] for r in valid_rows) / len(valid_rows) if valid_rows else 0.0
    low = low_roles(results, a.min_role_pass) if a.min_pass > 0 else {}
    for role, rate in low.items():
        print(f"GATE: {role} passed {rate:.2f}, below the per-role floor {a.min_role_pass:.2f}")
    if a.keep:
        print(f"fixtures kept in {scratch}")
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    if a.json:
        Path(a.json).write_text(json.dumps({"meta": meta, "overall_pass": overall, "low_roles": low,
                                            "fallback_configs": dead, "results": results}, indent=2))
    if a.min_pass > 0 and (dead or low):
        return 1  # a sweep that never ran its model, or a role that failed, must not pass the gate
    return 1 if overall < a.min_pass else 0


def row_line(row: dict, outs: list) -> str:
    if row["invalid"]:
        return (f"INVALID {row['role']:10} {row['case']:42} {row['config']:13} every trial fell back to "
                f"{', '.join(sorted({i for r in outs for i in r.get('served') or []})) or '?'} ${row['cost_usd']:.3f}")
    mark = "PASS" if row["pass_rate"] == 1 else ("FLAKY" if row["pass_rate"] > 0 else "FAIL")
    return (f"{mark:5} {row['role']:10} {row['case']:42} {row['config']:13} pass {row['pass_rate']:.2f} "
            f"consistent {row['consistency']:.2f} ${row['cost_usd']:.3f}"
            + ("" if row["pass_rate"] == 1 else f"  <- {[r for r in outs if not r.get('fell_back')][-1]['fails']}")
            + (f"  ({row['fell_back']} fell back, not counted)" if row["fell_back"] else "")
            + (f"  ({sum(1 for o_ in outs if o_.get('corrected'))} needed a corrective re-ask)"
               if any(o_.get("corrected") for o_ in outs) else ""))


ROLE_FLOOR = 0.5


def low_roles(results: list, floor: float) -> dict:
    """Roles whose mean pass rate over their valid rows is below `floor`: {"parent": 0.0}."""
    by = collections.defaultdict(list)
    for r in results:
        if not r["invalid"]:
            by[r["role"]].append(r["pass_rate"])
    return {role: sum(v) / len(v) for role, v in sorted(by.items()) if sum(v) / len(v) < floor}


def fallback_configs(results: list) -> dict:
    """Role and config pairs where every trial fell back: {"advisor fable/medium": [served ids]}."""
    by = collections.defaultdict(list)
    for r in results:
        by[f"{r['role']} {r['config']}"].append(r)
    return {c: sorted({i for r in rows for t in r["trials"] for i in t.get("served") or []})
            for c, rows in by.items() if all(r["invalid"] for r in rows)}


# Eval outputs: writing one doesn't change what the next run measures, so it doesn't make the tree dirty.
RUN_OUTPUTS = ("evals/runs/", "evals/last-run.json")


def run_meta(exclude=()) -> dict:
    """What produced this run, so saved runs can be compared: commit, dirty tree, CLI, date.
    `dirty` ignores eval outputs (RUN_OUTPUTS and the paths in `exclude`, such as --json)."""
    import claude_cli
    skip = set()
    for x in exclude:
        try:
            skip.add(Path(x).resolve().relative_to(ROOT).as_posix())
        except ValueError:
            pass  # outside the repo: git status can't list it

    def out(cmd, cwd=ROOT, strip=True):
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
            return (p.stdout.strip() if strip else p.stdout) if p.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    # -z: "XY path" entries split by NUL, a rename's old path in the entry after it; no quoting.
    status = out(["git", "status", "--porcelain", "-z", "--untracked-files=all"], strip=False)
    changed, entries = [], (status or "").split("\0")
    while entries:
        e = entries.pop(0)
        if len(e) > 3:
            changed.append(e[3:])
            if e[0] in "RC":
                entries.pop(0)
    changed = [c for c in changed if c not in skip and not c.startswith(RUN_OUTPUTS)]
    return {"git_sha": out(["git", "rev-parse", "HEAD"]), "dirty": None if status is None else bool(changed),
            "claude_version": out([claude_cli.claude_bin(), "--version"]),
            "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


if __name__ == "__main__":
    sys.exit(main())
