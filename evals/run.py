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

Costs real money: each manager/reviewer case is one Opus call per trial. Cases live in
evals/cases/<role>/<id>.json; see evals/README.md for the format. State is written to a
scratch ORCHESTRATE_DIR, never to your real one.
"""
from __future__ import annotations

import argparse
import collections
import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
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


# ------------------------------------------------------------------ roles ---
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
    acts = decision.get("actions") or []
    exp = case["expect"]
    kinds = [a.get("action") for a in acts] or ["none"]
    fails = []
    if exp.get("valid", True) and applied.get("errors"):
        fails.append(f"invalid actions: {applied['errors']}")
    if exp.get("any_action") and not set(kinds) & set(exp["any_action"]):
        fails.append(f"expected one of {exp['any_action']}, got {kinds}")
    if exp.get("no_action") and set(kinds) & set(exp["no_action"]):
        fails.append(f"must not {sorted(set(kinds) & set(exp['no_action']))}")
    for tid in exp.get("touches", []):
        if not any(a.get("task") == tid or (isinstance(a.get("task"), dict) and tid in json.dumps(a["task"]))
                   for a in acts):
            fails.append(f"expected an action on {tid}")
    for needle in exp.get("mentions", []):
        if needle.lower() not in json.dumps(decision).lower():
            fails.append(f"decision never mentions {needle!r}")
    label = ",".join(sorted(set(k for k in kinds if k != "none")) or ["none"])
    return {"ok": not fails, "label": label, "fails": fails, "cost": job.state["cost_usd"],
            "detail": decision.get("note"), "corrected": corrected}


def run_reviewer(o, case: dict, tmp: Path, config: str | None) -> dict:
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
    exp = case["expect"]
    fails = []
    if v.get("reviewer_error"):
        fails.append("reviewer produced no verdict")
    sev_rank = {"minor": 1, "major": 2, "blocker": 3}
    serious = [f for f in v.get("findings", []) if sev_rank.get(f.get("severity"), 0) >= 2]
    if exp.get("verdict") and v.get("verdict") != exp["verdict"]:
        fails.append(f"verdict {v.get('verdict')} != {exp['verdict']}")
    if exp.get("finding_mentions"):
        text = json.dumps(serious).lower()
        miss = [n for n in exp["finding_mentions"] if n.lower() not in text]
        if miss:
            fails.append(f"no blocker/major finding mentions {miss}")
    if exp.get("finding_file") and not any(exp["finding_file"] in str(f.get("file", "")) for f in serious):
        fails.append(f"no blocker/major finding on {exp['finding_file']}")
    return {"ok": not fails, "label": v.get("verdict"), "fails": fails, "cost": job.state["cost_usd"],
            "detail": v.get("summary")}


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


RUNNERS = {"manager": run_manager, "reviewer": run_reviewer, "classifier": run_classifier}


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
    ap.add_argument("--keep", action="store_true", help="keep the scratch fixture repos for inspection")
    a = ap.parse_args(argv)

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
            labels = collections.Counter(r["label"] for r in outs)
            row = {"role": role, "case": case["id"], "config": config or "default", "core": bool(case.get("core")),
                   "pass_rate": sum(r["ok"] for r in outs) / len(outs),
                   "consistency": labels.most_common(1)[0][1] / len(outs),
                   "cost_usd": round(sum(r["cost"] for r in outs), 4), "trials": outs}
            results.append(row)
            mark = "PASS" if row["pass_rate"] == 1 else ("FLAKY" if row["pass_rate"] > 0 else "FAIL")
            print(f"{mark:5} {role:10} {case['id']:42} {row['config']:13} pass {row['pass_rate']:.2f} "
                  f"consistent {row['consistency']:.2f} ${row['cost_usd']:.3f}"
                  + ("" if row["pass_rate"] == 1 else f"  <- {outs[-1]['fails']}")
                  + (f"  ({sum(1 for o_ in outs if o_.get('corrected'))} needed a corrective re-ask)"
                     if any(o_.get("corrected") for o_ in outs) else ""), flush=True)
    by = collections.defaultdict(list)
    for r in results:
        by[(r["role"], r["config"])].append(r)
    print()
    for (role, config), rows in sorted(by.items()):
        n = len(rows)
        print(f"{role:10} {config:13} pass {sum(r['pass_rate'] for r in rows) / n:.2f}  consistency "
              f"{sum(r['consistency'] for r in rows) / n:.2f}  cost ${sum(r['cost_usd'] for r in rows):.2f} "
              f"({n} cases x {a.trials} trials)")
    overall = sum(r["pass_rate"] for r in results) / len(results)
    if a.keep:
        print(f"fixtures kept in {scratch}")
    else:
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
    if a.json:
        Path(a.json).write_text(json.dumps({"overall_pass": overall, "results": results}, indent=2))
    return 1 if overall < a.min_pass else 0


if __name__ == "__main__":
    sys.exit(main())
