#!/usr/bin/env python3
"""orchestrate.py: plan -> split -> parallel subagents -> merge -> review, for repos and monorepos.

The PARENT agent (or you) does the thinking: writes a plan.json following the
orchestrate-plan skill. This script does everything mechanical and deterministic:

  capacity                 how many subagents this machine can run in parallel, and why
  scan     --repo R        monorepo map: packages, areas, verify commands, hotspots, setup
  plan     --repo R --goal "..."   (optional) have an Opus planner write plan.json for you
  validate PLAN            schema + DAG + file-ownership checks; prints waves and the cap
  brief    PLAN TASK       print the exact handoff a subagent will receive
  run      PLAN [--detach] [--approve ID ...] [--max-parallel N]
  resume   JOB  [--detach] [--approve ID ...]
  status   JOB             progress table (JOB may be omitted: latest job)
  wait     JOB [--timeout S]   block until the job finishes or S seconds pass
  clean    JOB [--branches]    remove worktrees (and task branches)
  scope-check --owns P ...     (used as a verify step) fail if changes leave owned paths

Execution model
  * Integration branch  orchestrate/<job>  (worktree) starts at the base commit.
  * A job-level baseline runs there first (broken environment -> stop before spending tokens).
  * Each task gets its own worktree + branch from the CURRENT integration head (so it
    sees its dependencies' merged work), runs setup, then run_task.py (model routing,
    retries, escalation, learning) with a scope check prepended to its verify steps.
  * Passing tasks are committed and merged (in order of completion) into the
    integration branch; a conflict is handed to an integrator subagent.
  * After all tasks: final verify -> reviewer subagent (fresh context, Opus) ->
    up to N fix rounds -> report. Nothing is merged into your base branch.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import router  # noqa: E402

CLAUDE = os.environ.get("ROUTER_CLAUDE_BIN", "claude")
JOBS = router.DIR / "jobs"
PLANS = router.DIR / "plans"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
EXIT = {"done": 0, "invalid": 2, "environment": 3, "failed": 4, "awaiting_approval": 5,
        "review_failed": 6, "budget": 7, "interrupted": 130}

# Defaults for capacity (override in plan.json or env)
MEM_PER_AGENT_GB = float(os.environ.get("ORCHESTRATE_MEM_PER_AGENT_GB", "1.5"))
CPU_PER_AGENT = float(os.environ.get("ORCHESTRATE_CPU_PER_AGENT", "1.0"))
HARD_MAX = int(os.environ.get("ORCHESTRATE_HARD_MAX", "6"))  # API rate limits / quota

LOCKFILE_SETUP = {
    "pnpm-lock.yaml": "pnpm install --frozen-lockfile --prefer-offline",
    "package-lock.json": "npm ci --prefer-offline --no-audit --no-fund",
    "yarn.lock": "yarn install --immutable",
    "bun.lockb": "bun install --frozen-lockfile",
    "bun.lock": "bun install --frozen-lockfile",
}
HOTSPOT_ANYWHERE = set(LOCKFILE_SETUP) | {"go.sum", "go.work", "go.work.sum", "Cargo.lock",
                                          "poetry.lock", "uv.lock", "Gemfile.lock"}
HOTSPOT_ROOT = {"package.json", "pnpm-workspace.yaml", "turbo.json", "nx.json", "lerna.json",
                "tsconfig.base.json", "tsconfig.json", "Makefile", ".env.example",
                "docker-compose.yml", "Cargo.toml", "go.mod"}
MANIFESTS = {"package.json": "node", "go.mod": "go", "Cargo.toml": "rust", "pyproject.toml": "python"}
SKIP_DIRS = {"node_modules", "vendor", "third_party", "third-party", "fixtures", "testdata",
             "__fixtures__", "examples", "dist", "build"}


def _skipped(path: str) -> bool:
    """Vendored, hidden or fixture paths are not packages/hotspots of this repo."""
    return any(seg.startswith(".") or seg in SKIP_DIRS for seg in Path(path).parts[:-1])


# =============================================================== utilities ===
def log(msg: str) -> None:
    print(f"[orchestrate {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def sh(cmd: str, cwd: Path, timeout: int = 1800, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, f"TIMEOUT after {timeout}s: {cmd}\n{e.stdout or ''}{e.stderr or ''}"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}:\n{p.stdout}{p.stderr}")
    return p.stdout.strip()


def git_ident(repo: Path) -> list[str]:
    if git(repo, "config", "user.email", check=False):
        return []
    return ["-c", "user.name=orchestrate", "-c", "user.email=orchestrate@localhost"]


def read_text(p) -> str | None:
    try:
        return Path(p).read_text()
    except OSError:
        return None


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def extract_json(text: str):
    """Last fenced ```json block, else the outermost {...} in text."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    for cand in reversed(blocks):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
    s, e = (text or "").find("{"), (text or "").rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            return None
    return None


def skill_text(name: str) -> str:
    """Body of a sibling support skill (orchestrate-plan / orchestrate-review)."""
    for base in (HERE.parents[1], Path.home() / ".agents/skills", Path.home() / ".claude/skills"):
        t = read_text(base / name / "SKILL.md")
        if t:
            return re.sub(r"^---\n.*?\n---\n", "", t, count=1, flags=re.S).strip()
    raise SystemExit(f"support skill {name!r} not found next to {HERE.parent}; "
                     f"install the whole repo (npx skills add thalhath2234/orchestrate-skill -g)")


def claude(prompt: str, model: str, effort: str, cwd: Path, *, perm: str, tools: list[str],
           resume: str | None = None, timeout: int = 3600, settings: dict | None = None) -> dict:
    cmd = [CLAUDE, "-p", prompt, "--model", model, "--effort", effort,
           "--output-format", "json", "--permission-mode", perm]
    if settings:
        cmd += ["--settings", json.dumps(settings)]
    if tools:
        cmd += ["--allowedTools", *tools]
    if resume:
        cmd += ["--resume", resume]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        data = json.loads(p.stdout.strip().splitlines()[-1])
    except subprocess.TimeoutExpired:
        return {"is_error": True, "result": f"timed out after {timeout}s"}
    except (json.JSONDecodeError, IndexError):
        return {"is_error": True, "result": (p.stdout + p.stderr)[-2000:]}
    return data


# ================================================================ capacity ===
def _cgroup() -> tuple[float | None, float | None]:
    """(memory headroom GB, cpu quota cores) from cgroup v2, tightest ancestor wins."""
    cg = read_text("/proc/self/cgroup")
    if not cg:
        return None, None
    rel = next((l.split(":", 2)[2] for l in cg.splitlines() if l.startswith("0::")), None)
    if rel is None:
        return None, None
    root = Path("/sys/fs/cgroup")
    p, headroom, cpu = root / rel.lstrip("/"), None, None
    while True:
        m, cur = read_text(p / "memory.max"), read_text(p / "memory.current")
        if m and m.strip() != "max" and cur:
            h = (int(m) - int(cur)) / 2**30
            headroom = h if headroom is None else min(headroom, h)
        c = read_text(p / "cpu.max")
        if c and not c.startswith("max"):
            q, per = c.split()[:2]
            cpu = int(q) / int(per) if cpu is None else min(cpu, int(q) / int(per))
        if p == root or p.parent == p:
            break
        p = p.parent
    return headroom, cpu


def _mem_available_gb() -> float | None:
    mi = read_text("/proc/meminfo")
    if mi:
        m = re.search(r"MemAvailable:\s+(\d+) kB", mi)
        return int(m.group(1)) / 2**20 if m else None
    if platform.system() == "Darwin":
        code, out = sh("vm_stat", Path.cwd(), 10)
        if code == 0:
            page = int(re.search(r"page size of (\d+)", out).group(1))
            pages = sum(int(re.search(rf"{k}:\s+(\d+)", out).group(1))
                        for k in ("Pages free", "Pages inactive", "Pages speculative")
                        if re.search(rf"{k}:\s+(\d+)", out))
            return pages * page / 2**30
    return None


def compute_cap(mem_gb: float | None, cpus: float, load: float, mem_per_agent: float,
                cpu_per_agent: float, hard_max: int, plan_max: int | None = None) -> dict:
    by_mem = math.floor(mem_gb * 0.8 / mem_per_agent) if mem_gb is not None else hard_max
    by_cpu = math.floor(max(cpus - load, 1.0) / cpu_per_agent)
    limits = {"memory": by_mem, "cpu": by_cpu, "hard_max": hard_max}
    if plan_max:
        limits["plan"] = plan_max
    cap = max(1, min(limits.values()))
    return {"cap": cap, "limited_by": min(limits, key=limits.get), "limits": limits}


def capacity(plan: dict | None = None, override: int | None = None) -> dict:
    plan = plan or {}
    mem_sys = _mem_available_gb()
    cg_mem, cg_cpu = _cgroup()
    mem = min(x for x in (mem_sys, cg_mem) if x is not None) if (mem_sys or cg_mem) else None
    try:
        cpus = float(len(os.sched_getaffinity(0)))
    except AttributeError:
        cpus = float(os.cpu_count() or 2)
    if cg_cpu:
        cpus = min(cpus, cg_cpu)
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
    mpa = float(plan.get("mem_per_agent_gb", MEM_PER_AGENT_GB))
    cpa = float(plan.get("cpu_per_agent", CPU_PER_AGENT))
    res = compute_cap(mem, cpus, load, mpa, cpa, HARD_MAX, override or plan.get("max_parallel"))
    res.update(mem_available_gb=None if mem is None else round(mem, 2),
               cgroup_mem_headroom_gb=None if cg_mem is None else round(cg_mem, 2),
               cpus=cpus, load_1m=round(load, 2), mem_per_agent_gb=mpa, cpu_per_agent=cpa)
    return res


def admission_ok(mem_per_agent: float) -> bool:
    """Live check before launching one more subagent."""
    mem_sys = _mem_available_gb()
    cg_mem, _ = _cgroup()
    vals = [x for x in (mem_sys, cg_mem) if x is not None]
    return not vals or min(vals) >= mem_per_agent * 0.8


# ==================================================================== scan ===
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32] or "root"


def scan(repo: Path) -> dict:
    repo = repo.resolve()
    all_files = git(repo, "ls-files").splitlines()
    files = [f for f in all_files if not _skipped(f)]
    fileset = set(files)
    pkgs = []
    for f in files:
        name = Path(f).name
        if name in MANIFESTS:
            d = str(Path(f).parent)
            pkgs.append({"dir": "" if d == "." else d, "kind": MANIFESTS[name], "manifest": f})
    nested = [p for p in pkgs if p["dir"]]
    root_pkgs = [p for p in pkgs if not p["dir"]]
    is_mono = len(nested) >= 2 or bool({"pnpm-workspace.yaml", "go.work", "turbo.json", "nx.json",
                                        "lerna.json"} & fileset)
    packages = nested if is_mono else (root_pkgs or nested)
    seen: dict[str, int] = {}
    pm = ("pnpm" if "pnpm-lock.yaml" in fileset else "yarn" if "yarn.lock" in fileset
          else "bun" if {"bun.lockb", "bun.lock"} & fileset else "npm")
    make_targets = _make_targets(repo / "Makefile")
    for p in packages:
        base = _slug(Path(p["dir"]).name if p["dir"] else repo.name)
        area = base if base not in seen else _slug(p["dir"].replace("/", "-"))
        seen[area] = 1
        p["area"] = area
        text = read_text(repo / p["manifest"]) or ""
        if p["kind"] == "node":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {}
            p["name"] = data.get("name")
            p["scripts"] = sorted(k for k in (data.get("scripts") or {})
                                  if re.match(r"^(test|lint|typecheck|type-check|check|build)(:|$)", k))
        elif p["kind"] == "go":
            m = re.search(r"^module\s+(\S+)", text, re.M)
            p["name"] = m.group(1) if m else None
        else:
            m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
            p["name"] = m.group(1) if m else None
        p["verify_suggestions"] = _verify_suggestions(p, make_targets, pm)
    agents_docs = [f for f in files if Path(f).name in ("AGENTS.md", "CLAUDE.md")]
    hotspots = sorted(f for f in files if Path(f).name in HOTSPOT_ANYWHERE
                      or ("/" not in f and f in HOTSPOT_ROOT))
    setup = [cmd for lock, cmd in LOCKFILE_SETUP.items() if lock in fileset][:1]
    root_verify = [f"make {t}" for t in ("lint", "typecheck", "test") if t in make_targets]
    return {"repo": str(repo), "monorepo": is_mono, "package_manager": pm if any(
        p["kind"] == "node" for p in pkgs) else None, "packages": packages,
        "agents_docs": agents_docs, "hotspots": hotspots, "suggested_setup": setup,
        "suggested_final_verify": root_verify,
        "make_targets": sorted(make_targets)[:120], "tracked_files": len(all_files)}


def _make_targets(makefile: Path) -> dict:
    text = read_text(makefile) or ""
    out = {}
    for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)[^\n#]*(?:##\s*(.*))?$", text, re.M):
        if not m.group(1).startswith("."):
            out[m.group(1)] = (m.group(2) or "").strip()
    return out


def _verify_suggestions(p: dict, targets: dict, pm: str) -> list[str]:
    area, out = p["area"], []
    for t in targets:
        if re.fullmatch(rf"(test|lint|typecheck|check|vet)[-_]{re.escape(area)}", t) or \
           re.fullmatch(rf"{re.escape(area)}[-_](test|lint|typecheck|check|vet)", t):
            out.append(f"make {t}")
    if out:
        return sorted(out)
    d = p["dir"] or "."
    if p["kind"] == "node" and p.get("scripts"):
        run = {"pnpm": f"pnpm --filter {p.get('name') or './' + d} run",
               "yarn": f"yarn workspace {p.get('name')} run", "bun": f"bun --cwd {d} run",
               "npm": f"npm --workspace {d} run"}[pm]
        return [f"{run} {s}" for s in p["scripts"] if not s.startswith("build")]
    if p["kind"] == "go":
        return [f"cd {d} && go vet ./... && go test ./..."]
    if p["kind"] == "rust":
        return [f"cargo test -p {p.get('name')}" if p.get("name") else f"cd {d} && cargo test"]
    if p["kind"] == "python":
        return [f"cd {d} && python -m pytest -q"]
    return []


# ============================================================== ownership ===
def _has_glob(p: str) -> bool:
    return any(c in p for c in "*?[")


def _pat_regex(p: str) -> re.Pattern:
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.compile("^" + out + "$")


def owned(path: str, pattern: str) -> bool:
    """`dir/` or `dir` (no glob) owns everything under it; `*` stays in a segment; `**` crosses."""
    if not _has_glob(pattern):
        pt = pattern.rstrip("/")
        return path == pt or path.startswith(pt + "/")
    return bool(_pat_regex(pattern.strip("/")).match(path))


def _static_prefix(p: str) -> str:
    segs = []
    for s in p.strip("/").split("/"):
        if _has_glob(s):
            break
        segs.append(s)
    return "/".join(segs)


def _nested(a: str, b: str) -> bool:
    return a == b or not a or not b or a.startswith(b + "/") or b.startswith(a + "/")


def _probes(prefix: str, pattern: str) -> list[str]:
    last = pattern.rstrip("/").split("/")[-1]
    leaf = re.sub(r"\*+|\?", "x", last) if _has_glob(last) else "__probe__"
    base = prefix + "/" if prefix else ""
    return [base + "__probe__", base + leaf, base + "d/" + leaf, base + "d/__probe__"]


def patterns_overlap(a: str, b: str, files: list[str]) -> bool:
    """Could one file (existing or new) be owned by both patterns?"""
    if any(owned(f, a) and owned(f, b) for f in files):
        return True
    pa, pb = _static_prefix(a), _static_prefix(b)
    if not _nested(pa, pb):
        return False
    if not _has_glob(a) and not _has_glob(b):
        return True
    return any(owned(pr, a) and owned(pr, b)
               for pref in {pa, pb} for pat in (a, b) for pr in _probes(pref, pat))


def scope_violations(cwd: Path, owns: list[str]) -> list[str]:
    # Don't use git() here: its .strip() eats the leading space of the first
    # " M path" line, shifting every path by one character.
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                         cwd=cwd, capture_output=True, text=True).stdout
    bad = []
    for line in out.splitlines():
        path = line[3:]
        paths = path.split(" -> ") if " -> " in path else [path]
        for p in paths:
            p = p.strip('"')
            if not any(owned(p, pat) for pat in owns):
                bad.append(p)
    return bad


# ============================================================== validation ===
def load_plan(path: Path) -> dict:
    try:
        return json.loads(Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"cannot read plan {path}: {e}")


def ancestors(tasks: dict, tid: str) -> set[str]:
    seen, stack = set(), list(tasks[tid].get("depends_on", []))
    while stack:
        d = stack.pop()
        if d in seen or d not in tasks:
            continue
        seen.add(d)
        stack.extend(tasks[d].get("depends_on", []))
    return seen


def validate(plan: dict) -> tuple[list[str], list[str], list[list[str]]]:
    errs, warns = [], []
    for k in ("job", "goal", "repo", "verify", "tasks"):
        if not plan.get(k):
            errs.append(f"plan.{k} is required")
    if errs:
        return errs, warns, []
    if not SLUG.match(plan["job"]):
        errs.append("plan.job must be a lowercase slug (a-z0-9-, max 41 chars)")
    repo = Path(plan["repo"]).expanduser()
    if not (repo / ".git").exists():
        errs.append(f"plan.repo {repo} is not a git checkout")
        return errs, warns, []
    if plan.get("base") and not git(repo, "rev-parse", "--verify", "--quiet", plan["base"], check=False):
        errs.append(f"plan.base {plan['base']!r} does not exist")
    for k in ("verify", "setup", "acceptance"):
        if k in plan and not isinstance(plan[k], list):
            errs.append(f"plan.{k} must be a list")
    tasks = {}
    for i, t in enumerate(plan["tasks"]):
        tid = t.get("id", f"#{i}")
        if not SLUG.match(str(tid)):
            errs.append(f"task {tid}: id must be a lowercase slug")
        if tid in tasks:
            errs.append(f"task {tid}: duplicate id")
        tasks[tid] = t
        for k in ("title", "goal", "type", "tier", "owns", "done", "verify"):
            if not t.get(k):
                errs.append(f"task {tid}: {k} is required")
        if t.get("tier") and t["tier"] not in router.TIER_DEFAULT:
            errs.append(f"task {tid}: tier must be one of {list(router.TIER_DEFAULT)}")
        if t.get("type"):
            try:
                router.validate_type(t["type"])
            except SystemExit as e:
                errs.append(f"task {tid}: {e}")
        for p in t.get("owns", []) + t.get("reads", []):
            if p.startswith("/") or ".." in Path(p).parts:
                errs.append(f"task {tid}: path {p!r} must be repo-relative")
        if t.get("type", "").startswith("migration:") and not t.get("gate"):
            warns.append(f"task {tid}: migrations usually deserve a \"gate\" (human approval)")
        if t.get("tier") == "T3":
            warns.append(f"task {tid}: T3 subtask; consider splitting it further")
        if len(t.get("done", [])) and not any(re.search(r"\w{4}", d) for d in t["done"]):
            warns.append(f"task {tid}: done criteria look empty")
    for tid, t in tasks.items():
        for d in t.get("depends_on", []):
            if d not in tasks:
                errs.append(f"task {tid}: depends_on unknown task {d!r}")
    # cycles + waves (Kahn)
    indeg = {t: len([d for d in tasks[t].get("depends_on", []) if d in tasks]) for t in tasks}
    waves, ready = [], sorted(t for t, n in indeg.items() if n == 0)
    done = set()
    while ready:
        waves.append(ready)
        done |= set(ready)
        nxt = []
        for t in tasks:
            if t not in done and t not in nxt and all(d in done for d in tasks[t].get("depends_on", [])):
                nxt.append(t)
        ready = sorted(nxt)
    if len(done) != len(tasks):
        errs.append(f"dependency cycle among: {sorted(set(tasks) - done)}")
    if errs:
        return errs, warns, waves
    # ownership: tasks that can run concurrently must not overlap
    files = git(repo, "ls-files").splitlines()
    ids = list(tasks)
    anc = {t: ancestors(tasks, t) for t in ids}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ordered = a in anc[b] or b in anc[a]
            for pa in tasks[a]["owns"]:
                for pb in tasks[b]["owns"]:
                    if patterns_overlap(pa, pb, files):
                        msg = f"tasks {a} and {b} both own {pa!r} / {pb!r}"
                        (warns if ordered else errs).append(
                            msg + (" (ok: they run in order)" if ordered else
                                   "; make one depend on the other or split ownership"))
    hot = scan_hotspots(repo, files)
    for h in hot:
        owners = [t for t in ids if any(owned(h, p) for p in tasks[t]["owns"])]
        if len(owners) > 1:
            warns.append(f"hotspot {h} is owned by several tasks: {owners}")
    if len(tasks) > 12:
        warns.append(f"{len(tasks)} tasks: coordination overhead grows; merge small ones")
    if plan.get("verify") and len(tasks) == 1:
        warns.append("single-task plan: run_task.py is simpler for this")
    return errs, warns, waves


def scan_hotspots(repo: Path, files: list[str]) -> list[str]:
    return [f for f in files if not _skipped(f) and (
        Path(f).name in HOTSPOT_ANYWHERE or ("/" not in f and f in HOTSPOT_ROOT))]


# =================================================================== brief ===
def agents_docs_for(repo: Path, owns: list[str]) -> list[str]:
    found = []
    for p in owns:
        d = Path(_static_prefix(p))
        chain = [d, *d.parents] if str(d) not in ("", ".") else [Path(".")]
        for c in chain:
            for name in ("AGENTS.md", "CLAUDE.md"):
                f = (Path(c) / name)
                if (repo / f).is_file() and str(f) not in found:
                    found.append(str(f))
    return sorted(found, key=lambda s: (s.count("/"), s))


def build_brief(plan: dict, task: dict, repo: Path, upstream: dict) -> str:
    docs = agents_docs_for(repo, task["owns"])
    reads = [r for r in task.get("reads", []) if r not in docs]
    L = [f"# Subtask {task['id']}: {task['title']}",
         f"Task id: {task['id']}",
         f"Part of job \"{plan['job']}\": {plan['goal']}", "",
         "## Your goal", task["goal"].strip(), "",
         "## You own (the ONLY paths you may create, edit or delete)",
         *[f"- `{p}`" for p in task["owns"]], ""]
    if docs or reads:
        L += ["## Read first (context, do not edit)", *[f"- `{p}`" for p in docs + reads], ""]
    if task.get("depends_on"):
        L += ["## Already done by upstream tasks (merged into your starting point)"]
        for d in task["depends_on"]:
            u = upstream.get(d, {})
            r = u.get("report") or {}
            L += [f"### {d}: {u.get('title', '')}",
                  f"- Summary: {r.get('summary', '(no report)')}"]
            if r.get("interfaces"):
                L.append(f"- Interfaces: {r['interfaces']}")
            if r.get("notes_for_dependents"):
                L.append(f"- Notes for you: {r['notes_for_dependents']}")
            if u.get("files"):
                L.append(f"- Files changed: {', '.join(u['files'][:30])}")
        L.append("")
    if task.get("interfaces"):
        L += ["## Interfaces you must provide or respect", task["interfaces"].strip(), ""]
    L += ["## Done looks like", *[f"- {d}" for d in task["done"]],
          "- These commands pass (run them yourself before finishing):",
          *[f"  - `{c}`" for c in task["verify"]],
          "- `git status` shows changes only inside the paths you own.", "",
          "## Rules",
          "- Stay inside your owned paths. If the job truly needs a change elsewhere (a shared type, "
          "a lockfile, a route registry), do NOT make it: put it in `open_issues`.",
          "- Do not add or upgrade dependencies unless you own the lockfile.",
          "- Do not commit, push, rebase or switch branches; the orchestrator commits your work.",
          "- Other agents are working on other parts of this repo in parallel, in other worktrees.",
          "- Don't start long-running dev servers. If you must, add $ORCHESTRATE_SLOT to default ports.",
          "- Prefer the repo's existing patterns; read the nearest AGENTS.md before editing.", "",
          "## Report",
          "End your final message with exactly one fenced JSON block:",
          "```json",
          '{"summary": "what you changed, 1-3 sentences", "interfaces": "types/endpoints/functions '
          'others can now use", "notes_for_dependents": "what downstream tasks must know", '
          '"open_issues": ["anything you could not do inside your scope"]}',
          "```"]
    return "\n".join(L)


# ===================================================================== job ===
class Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.dir = JOBS / job_id
        self.file = self.dir / "job.json"
        self.lock = threading.Lock()
        self.state: dict = json.loads(self.file.read_text()) if self.file.exists() else {}

    def save(self) -> None:
        with self.lock:
            write_json(self.file, self.state)

    @property
    def plan(self) -> dict:
        return json.loads((self.dir / "plan.json").read_text())


def latest_job() -> str:
    jobs = sorted(JOBS.glob("*/job.json"), key=lambda p: p.stat().st_mtime)
    if not jobs:
        raise SystemExit("no jobs yet")
    return jobs[-1].parent.name


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def start_job(plan: dict, plan_path: Path) -> Job:
    job = Job(plan["job"])
    job.dir.mkdir(parents=True, exist_ok=True)
    (job.dir / "briefs").mkdir(exist_ok=True)
    (job.dir / "logs").mkdir(exist_ok=True)
    if job.state and job.state.get("status") == "done":
        raise SystemExit(f"job {job.id} already finished; pick a new plan.job or `clean {job.id}`")
    write_json(job.dir / "plan.json", plan)
    repo = Path(plan["repo"]).expanduser().resolve()
    if not job.state:
        if git(repo, "rev-parse", "--verify", "--quiet", f"orchestrate/{job.id}", check=False):
            raise SystemExit(f"branch orchestrate/{job.id} already exists from an earlier run. "
                             f"Pick a new plan.job, or delete it: git -C {repo} branch -D orchestrate/{job.id}")
        base = plan.get("base") or git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        job.state = {
            "job": job.id, "repo": str(repo), "base": base,
            "base_sha": git(repo, "rev-parse", base), "branch": f"orchestrate/{job.id}",
            "integration_wt": str(job.dir / "wt" / "_integration"), "created": time.time(),
            "status": "running", "approved": [], "cost_usd": 0.0, "review": {"rounds": []},
            "tasks": {t["id"]: {"status": "pending", "title": t["title"], "type": t["type"],
                                "tier": t["tier"]} for t in plan["tasks"]},
        }
    else:  # resume: add new tasks, reset interrupted ones
        for t in plan["tasks"]:
            st = job.state["tasks"].setdefault(t["id"], {"status": "pending", "title": t["title"],
                                                         "type": t["type"], "tier": t["tier"]})
            if st["status"] in ("running", "awaiting_approval", "blocked", "failed"):
                st["status"] = "pending"
        job.state["status"] = "running"
    job.save()
    return job


def ensure_integration(job: Job) -> Path:
    repo, wt = Path(job.state["repo"]), Path(job.state["integration_wt"])
    br = job.state["branch"]
    if wt.exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "prune")
    if git(repo, "rev-parse", "--verify", "--quiet", br, check=False):
        git(repo, "worktree", "add", str(wt), br)
    else:
        git(repo, "worktree", "add", "-b", br, str(wt), job.state["base_sha"])
    return wt


def run_setup(cmds: list[str], cwd: Path, logf: Path) -> tuple[bool, str]:
    for c in cmds:
        code, out = sh(c, cwd, 1800)
        with open(logf, "a") as fh:
            fh.write(f"$ {c}\n{out}\n")
        if code != 0:
            return False, f"setup failed: {c}\n{out[-3000:]}"
    return True, ""


def prepare_worktree(job: Job, tid: str, start_sha: str) -> tuple[Path, str]:
    """Main thread only: git worktree/branch operations are not safe to run concurrently."""
    repo = Path(job.state["repo"])
    wt = job.dir / "wt" / tid
    br = f"orchestrate/{job.id}--{tid}"
    if wt.exists():
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(wt, ignore_errors=True)
    git(repo, "worktree", "prune")
    git(repo, "branch", "-D", br, check=False)
    git(repo, "worktree", "add", "-q", "-b", br, str(wt), start_sha)
    return wt, br


def exec_task(job: Job, plan: dict, task: dict, wt: Path, br: str, slot: int) -> dict:
    """Runs in a worker thread: setup, handoff brief, run_task.py (routing/retries/escalation)."""
    logf = job.dir / "logs" / f"{task['id']}.log"
    ok, err = run_setup(plan.get("setup", []), wt, logf)
    if not ok:
        return {"ok": False, "result": "environment", "error": err, "wt": str(wt), "branch": br}
    upstream = {d: job.state["tasks"].get(d, {}) for d in task.get("depends_on", [])}
    brief = build_brief(plan, task, wt, upstream)
    (job.dir / "briefs" / f"{task['id']}.md").write_text(brief)
    scope = (f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'orchestrate.py'))} scope-check "
             + " ".join(f"--owns {shlex.quote(p)}" for p in task["owns"]))
    cmd = [sys.executable, str(HERE / "run_task.py"), "--repo", str(wt), "--type", task["type"],
           "--tier", task["tier"], "--verify", scope]
    for v in task["verify"]:
        cmd += ["--verify", v]
    if isinstance(task.get("baseline"), list):
        for b in task["baseline"]:
            cmd += ["--baseline", b]
    else:
        cmd += ["--no-baseline"]  # the job-level baseline already ran
    if plan.get("extra_tools"):
        cmd += ["--extra-tools", *plan["extra_tools"]]
    if plan.get("sandbox"):
        cmd.append("--sandbox")
    cmd.append(brief)
    env = {**os.environ, "ORCHESTRATE_SLOT": str(slot), "ORCHESTRATE_JOB": job.id,
           "ORCHESTRATE_TASK": task["id"]}
    with open(logf, "a") as fh:
        p = subprocess.run(cmd, cwd=wt, stdout=subprocess.PIPE, stderr=fh, text=True, env=env)
    summary = extract_json(p.stdout) or {}
    report = extract_json(summary.get("final_message", "")) or {}
    cost = sum((s.get("cost_usd") or 0) + (s.get("classifier_cost_usd") or 0)
               for s in summary.get("steps", []))
    return {"ok": p.returncode == 0, "result": summary.get("result", f"exit {p.returncode}"),
            "steps": summary.get("steps", []), "proposals": summary.get("proposals", []),
            "report": report, "cost_usd": cost, "wt": str(wt), "branch": br}


def commit_and_merge(job: Job, plan: dict, task: dict, res: dict) -> tuple[bool, str]:
    repo, wt = Path(job.state["repo"]), Path(res["wt"])
    integ = Path(job.state["integration_wt"])
    ident = git_ident(repo)
    git(wt, "add", "-A")
    if git(wt, "diff", "--cached", "--name-only"):
        git(wt, *ident, "commit", "-q", "-m", f"orchestrate({job.id}): {task['id']}: {task['title']}")
    files = git(wt, "diff", "--name-only", f"{job.state['tasks'][task['id']]['start_sha']}..HEAD").splitlines()
    if files:
        p = subprocess.run(["git", *ident, "merge", "--no-ff", "--no-edit", "-m",
                            f"orchestrate({job.id}): merge {task['id']}", res["branch"]],
                           cwd=integ, capture_output=True, text=True)
        if p.returncode != 0:
            ok, why = resolve_conflicts(job, plan, task, ident)
            if not ok:
                git(integ, "merge", "--abort", check=False)
                return False, f"merge conflict not resolved: {why}"
    git(repo, "worktree", "remove", "--force", str(wt), check=False)
    return True, json.dumps(files)


def resolve_conflicts(job: Job, plan: dict, task: dict, ident: list[str]) -> tuple[bool, str]:
    integ = Path(job.state["integration_wt"])
    conflicted = git(integ, "diff", "--name-only", "--diff-filter=U").splitlines()
    log(f"merge conflict for {task['id']} in {conflicted}; handing to integrator subagent")
    reports = {t: s.get("report") for t, s in job.state["tasks"].items() if s.get("report")}
    prompt = f"""ORCHESTRATE_MERGE
{skill_text('orchestrate-review')}

---
You are the INTEGRATOR for job "{job.id}": {plan['goal']}
Merging task "{task['id']}" ({task['title']}) into the integration branch produced conflicts in:
{chr(10).join('- ' + c for c in conflicted)}

Task reports (what each side intended):
{json.dumps(reports, indent=2)[:6000]}

Resolve every conflict so BOTH intents survive. Edit only the conflicted files and remove all
conflict markers. Do not run git, do not commit, do not touch other files.
End with a JSON block: {{"resolved": true|false, "notes": "..."}}"""
    res = claude(prompt, "sonnet", "high", integ, perm="acceptEdits", tools=["Read", "Grep", "Glob"],
                 settings={"permissions": {"blockWritesOutsideWorkingDirectories": True}})
    left = git(integ, "diff", "--name-only", "--diff-filter=U").splitlines()
    markers = [f for f in conflicted if re.search(r"^(<<<<<<<|>>>>>>>) ", read_text(integ / f) or "", re.M)]
    if left or markers:
        return False, f"still conflicted: {left or markers}; agent said: {str(res.get('result'))[-300:]}"
    git(integ, "add", "-A")
    git(integ, *ident, "commit", "-q", "--no-edit")
    return True, "resolved"


def review_phase(job: Job, plan: dict) -> str:
    integ = Path(job.state["integration_wt"])
    ident = git_ident(Path(job.state["repo"]))
    rounds = int(plan.get("review_rounds", 2))
    area = plan.get("area", "cross")
    union = sorted({p for t in plan["tasks"] for p in t["owns"]} | set(plan.get("review_may_edit", [])))
    for r in range(1, rounds + 1):
        code, vout = 0, ""
        for c in plan["verify"]:
            code, out = sh(c, integ, 1800)
            vout += f"$ {c}\n{out[-4000:]}\n"
            if code:
                break
        rnd = {"round": r, "final_verify_ok": code == 0, "final_verify_tail": vout[-3000:]}
        job.state["review"]["rounds"].append(rnd)
        job.save()
        verdict = {"verdict": "changes_requested", "summary": "final verify failed",
                   "findings": [{"severity": "blocker", "issue": "final verify failed",
                                 "evidence": vout[-1500:]}]} if code else review_once(job, plan, vout)
        rnd["verdict"] = verdict
        job.save()
        blocking = [f for f in verdict.get("findings", []) if f.get("severity") in ("blocker", "major")]
        if verdict.get("verdict") == "pass" or not blocking:
            return "done"
        if r == rounds:
            break
        log(f"review round {r}: {len(blocking)} blocking finding(s); running a fix subagent")
        scope = (f"{shlex.quote(sys.executable)} {shlex.quote(str(HERE / 'orchestrate.py'))} scope-check "
                 + " ".join(f"--owns {shlex.quote(p)}" for p in union))
        task = ("Fix these review findings on the integration branch of job "
                f"\"{job.id}\" ({plan['goal']}). Do not commit.\n\n"
                + json.dumps(blocking, indent=2)
                + "\n\nAcceptance criteria for the whole job:\n"
                + "\n".join(f"- {a}" for a in plan.get("acceptance", [])))
        cmd = [sys.executable, str(HERE / "run_task.py"), "--repo", str(integ), "--type",
               f"bugfix:{area}", "--tier", "T2", "--no-baseline", "--max-escalations", "1",
               "--verify", scope, *sum((["--verify", v] for v in plan["verify"]), []), task]
        with open(job.dir / "logs" / f"_fix-round-{r}.log", "a") as fh:
            p = subprocess.run(cmd, cwd=integ, stdout=subprocess.PIPE, stderr=fh, text=True)
        s = extract_json(p.stdout) or {}
        job.state["cost_usd"] += sum((x.get("cost_usd") or 0) for x in s.get("steps", []))
        rnd["fix"] = {"result": s.get("result"), "steps": s.get("steps")}
        git(integ, "add", "-A")
        if git(integ, "diff", "--cached", "--name-only"):
            git(integ, *ident, "commit", "-q", "-m", f"orchestrate({job.id}): review fixes round {r}")
        job.save()
    return "review_failed"


def review_once(job: Job, plan: dict, verify_out: str) -> dict:
    integ = Path(job.state["integration_wt"])
    base = job.state["base_sha"]
    stat = git(integ, "diff", "--stat", f"{base}..HEAD")
    diff = git(integ, "diff", f"{base}..HEAD")
    limit = int(plan.get("review_diff_chars", 120000))
    diff_txt = diff if len(diff) <= limit else (diff[:limit] + f"\n... [diff truncated at {limit} chars; "
                                                "Read the remaining changed files listed in the stat]")
    reports = {t: {"title": s.get("title"), "report": s.get("report")} for t, s in job.state["tasks"].items()}
    tasks = [{k: t.get(k) for k in ("id", "title", "goal", "owns", "done")} for t in plan["tasks"]]
    prompt = f"""ORCHESTRATE_REVIEW
{skill_text('orchestrate-review')}

---
You are the REVIEWER for job "{job.id}" with a fresh context. Goal: {plan['goal']}
The full diff against the base commit is below; Read any file for more context. You cannot run
commands: the job's verify commands already ran and passed (output below). Judge by reading.

Job acceptance criteria:
{chr(10).join('- ' + a for a in plan.get('acceptance', [])) or '- (none given: judge against the goal)'}

Subtasks (what each was asked to do):
{json.dumps(tasks, indent=2)[:8000]}

What the subagents reported:
{json.dumps(reports, indent=2)[:8000]}

Diff stat:
{stat[-4000:]}

DIFF:
{diff_txt}

Final verify output (it passed):
{verify_out[-2000:]}

Reply with ONLY the JSON object described in "Reviewer output"."""
    res = claude(prompt, "opus", "high", integ, perm="dontAsk", tools=["Read", "Grep", "Glob"])
    job.state["cost_usd"] += res.get("total_cost_usd") or 0
    v = extract_json(str(res.get("result", "")))
    if not v or v.get("verdict") not in ("pass", "changes_requested"):
        return {"verdict": "changes_requested", "summary": "reviewer output unparseable",
                "findings": [{"severity": "minor", "issue": str(res.get("result"))[-500:]}]}
    return v


def guard_snapshot() -> str:
    """Fingerprint of the orchestrate skill files: subagents must never modify them."""
    import hashlib
    h = hashlib.sha256()
    for d in (HERE.parent, HERE.parents[1] / "orchestrate-plan", HERE.parents[1] / "orchestrate-review"):
        for f in sorted(d.rglob("*")) if d.is_dir() else []:
            if f.is_file() and "__pycache__" not in f.parts:
                h.update(str(f.relative_to(d.parent)).encode())
                h.update(f.read_bytes())
    return h.hexdigest()


def mark_blocked(job: Job, plan: dict) -> None:
    tasks = {t["id"]: t for t in plan["tasks"]}
    changed = True
    while changed:
        changed = False
        for tid, t in tasks.items():
            st = job.state["tasks"][tid]
            if st["status"] == "pending" and any(
                    job.state["tasks"][d]["status"] in ("failed", "blocked") for d in t.get("depends_on", [])):
                st["status"] = "blocked"
                changed = True


def run_job(job: Job, plan: dict, approve: list[str], max_parallel: int | None,
            skip_baseline: bool) -> int:
    job.state["pid"] = os.getpid()
    job.state["approved"] = sorted(set(job.state.get("approved", [])) | set(approve))
    job.save()
    repo = Path(job.state["repo"])
    if git(repo, "status", "--porcelain"):
        log("note: your checkout has uncommitted changes; the job starts from the last commit "
            f"({job.state['base_sha'][:10]}) and won't include them")
    integ = ensure_integration(job)
    logf = job.dir / "logs" / "_integration.log"
    ok, err = run_setup(plan.get("setup", []), integ, logf)
    if not ok:
        return finish(job, plan, "environment", err)
    if not skip_baseline and not job.state.get("baseline_ok"):
        for c in plan.get("baseline", plan["verify"]):
            code, out = sh(c, integ, 1800)
            if code:
                return finish(job, plan, "environment",
                              f"job baseline fails BEFORE any change: `{c}`\n{out[-3000:]}\n"
                              "Fix the environment, or set plan.baseline to commands that pass now.")
        job.state["baseline_ok"] = True
        job.save()

    guard = guard_snapshot()
    main_before = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))
    cap_info = capacity(plan, max_parallel)
    cap = cap_info["cap"]
    job.state["capacity"] = cap_info
    job.save()
    log(f"parallel cap = {cap} (limited by {cap_info['limited_by']}: {cap_info['limits']})")
    tasks = {t["id"]: t for t in plan["tasks"]}
    budget = plan.get("budget_usd")
    running: dict[cf.Future, str] = {}
    free_slots = list(range(cap))
    pool = cf.ThreadPoolExecutor(max_workers=cap)
    stop_reason = None
    try:
        while True:
            mark_blocked(job, plan)
            ready = [t for t in tasks if job.state["tasks"][t]["status"] in ("pending", "awaiting_approval")
                     and all(job.state["tasks"][d]["status"] == "done" for d in tasks[t].get("depends_on", []))]
            launched = False
            for tid in ready:
                if len(running) >= cap:
                    break
                st = job.state["tasks"][tid]
                if tasks[tid].get("gate") and tid not in job.state["approved"]:
                    if st["status"] != "awaiting_approval":
                        log(f"{tid} needs approval: {tasks[tid]['gate']}")
                    st["status"] = "awaiting_approval"
                    continue
                if budget and job.state["cost_usd"] >= budget:
                    stop_reason = "budget"
                    break
                if running and not admission_ok(float(plan.get("mem_per_agent_gb", MEM_PER_AGENT_GB))):
                    break  # low memory right now: wait for a running task to finish
                slot = free_slots.pop(0)
                start_sha = git(integ, "rev-parse", "HEAD")
                wt, br = prepare_worktree(job, tid, start_sha)
                st.update(status="running", started=time.time(), start_sha=start_sha, slot=slot)
                job.save()
                log(f"start {tid} [{tasks[tid]['type']} {tasks[tid]['tier']}] slot {slot}")
                running[pool.submit(exec_task, job, plan, tasks[tid], wt, br, slot)] = tid
                launched = True
            job.save()
            if not running:
                break
            if stop_reason and not running:
                break
            done_set, _ = cf.wait(list(running), timeout=None if launched or not ready else 30,
                                  return_when=cf.FIRST_COMPLETED)
            for fut in done_set:
                tid = running.pop(fut)
                st = job.state["tasks"][tid]
                free_slots.append(st["slot"])
                free_slots.sort()
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = {"ok": False, "result": "error", "error": repr(e)}
                job.state["cost_usd"] += res.get("cost_usd") or 0
                if guard_snapshot() != guard:
                    st.update(status="failed", error="modified the orchestrate skill files")
                    job.save()
                    for f2, t2 in running.items():
                        job.state["tasks"][t2]["status"] = "pending"
                    return finish(job, plan, "failed",
                                  f"SECURITY: a subagent ({tid} or one running alongside it) modified "
                                  f"the orchestrate skill files in {HERE.parent}. Job stopped. "
                                  "Inspect them (git diff in the skill's repo) before resuming.")
                now_main = (git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"))
                if now_main != main_before:
                    job.state.setdefault("warnings", []).append(
                        f"your main checkout changed while {tid} ran; make sure those edits are yours")
                    main_before = now_main
                st.update(ended=time.time(), result=res.get("result"), steps=res.get("steps"),
                          report=res.get("report"), proposals=res.get("proposals"),
                          cost_usd=res.get("cost_usd"), error=res.get("error"))
                if res.get("ok"):
                    try:
                        ok, info = commit_and_merge(job, plan, tasks[tid], res)
                    except Exception as e:  # noqa: BLE001
                        ok, info = False, f"commit/merge error: {e}"
                    if ok:
                        st.update(status="done", files=json.loads(info))
                        log(f"done  {tid} ({len(st['files'])} files merged)")
                    else:
                        st.update(status="failed", error=info)
                        log(f"FAIL  {tid}: {info}")
                else:
                    st["status"] = "failed"
                    log(f"FAIL  {tid}: {res.get('result')} {res.get('error') or ''}".strip())
                job.save()
    except KeyboardInterrupt:
        for fut, tid in running.items():
            job.state["tasks"][tid]["status"] = "pending"
        return finish(job, plan, "interrupted", "interrupted by user")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    statuses = {t: s["status"] for t, s in job.state["tasks"].items() if t in tasks}
    if any(s in ("failed", "blocked") for s in statuses.values()):
        bad = {t: s for t, s in statuses.items() if s in ("failed", "blocked")}
        env = [t for t in bad if job.state["tasks"][t].get("result") == "environment"]
        msg = (f"tasks failed/blocked: {bad}. Logs: {job.dir / 'logs'}. "
               f"Fix and `orchestrate.py resume {job.id}` (done tasks are kept).")
        return finish(job, plan, "environment" if env and len(env) == len(
            [t for t in bad if bad[t] == "failed"]) else "failed", msg)
    if stop_reason == "budget" or (budget and any(s == "pending" for s in statuses.values())):
        return finish(job, plan, "budget", f"budget ${budget} reached")
    if any(s == "awaiting_approval" for s in statuses.values()):
        waiting = [t for t, s in statuses.items() if s == "awaiting_approval"]
        return finish(job, plan, "awaiting_approval",
                      f"waiting for approval: {waiting}. Resume with: orchestrate.py resume {job.id} "
                      + " ".join(f"--approve {w}" for w in waiting))
    log("all tasks merged; final verify + review")
    return finish(job, plan, review_phase(job, plan), "")


def finish(job: Job, plan: dict, status: str, message: str) -> int:
    job.state.update(status=status, message=message, finished=time.time())
    job.save()
    report = render_report(job, plan)
    (job.dir / "report.md").write_text(report)
    print(report)
    return EXIT.get(status, 1)


def render_report(job: Job, plan: dict) -> str:
    s = job.state
    L = [f"# orchestrate job `{job.id}`: {s['status'].upper()}", "", f"Goal: {plan['goal']}",
         f"Branch: `{s['branch']}` (base `{s['base']}` @ {s['base_sha'][:10]})",
         f"Cost: ${s.get('cost_usd', 0):.2f}"]
    if s.get("capacity"):
        c = s["capacity"]
        L.append(f"Parallel cap: {c['cap']} (limited by {c['limited_by']})")
    for w in s.get("warnings", []):
        L.append(f"Warning: {w}")
    if s.get("message"):
        L += ["", f"> {s['message'].splitlines()[0]}"] + [f"> {x}" for x in s["message"].splitlines()[1:12]]
    L += ["", "| Task | Type / tier | Status | Model path | Cost | Summary |", "|---|---|---|---|---|---|"]
    for t in plan["tasks"]:
        st = s["tasks"].get(t["id"], {})
        path = " → ".join(x.get("config", "?") for x in st.get("steps") or []) or "-"
        summ = ((st.get("report") or {}).get("summary") or st.get("error") or "").replace("\n", " ")[:120]
        L.append(f"| {t['id']} | {t['type']} {t['tier']} | {st.get('status')} | {path} | "
                 f"${(st.get('cost_usd') or 0):.2f} | {summ} |")
    issues = [(t, i) for t, st in s["tasks"].items() for i in ((st.get("report") or {}).get("open_issues") or [])]
    if issues:
        L += ["", "Open issues reported by subagents:", *[f"- **{t}**: {i}" for t, i in issues]]
    for r in s.get("review", {}).get("rounds", []):
        v = r.get("verdict") or {}
        L += ["", f"Review round {r['round']}: final verify {'ok' if r.get('final_verify_ok') else 'FAILED'}, "
                  f"verdict **{v.get('verdict', '-')}**: {v.get('summary', '')}"]
        L += [f"- [{f.get('severity')}] {f.get('file', '')} {f.get('issue', '')}" for f in v.get("findings", [])[:15]]
    props = router.pending()
    if props:
        L += ["", f"{len(props)} routing proposal(s) pending: `router.py proposals` (user approves)"]
    if s["status"] == "done":
        L += ["", "Next: review and merge it yourself:",
              f"    git -C {s['repo']} log --oneline {s['base_sha'][:10]}..{s['branch']}",
              f"    git -C {s['repo']} diff {s['base_sha'][:10]}...{s['branch']}",
              f"    git -C {s['repo']} merge --no-ff {s['branch']}   # when happy",
              f"    orchestrate.py clean {job.id}"]
    return "\n".join(L)


# ===================================================================== cli ===
def cmd_plan(a) -> int:
    repo = Path(a.repo).expanduser().resolve()
    info = scan(repo)
    cap = capacity()
    schema_hint = skill_text("orchestrate-plan")
    prompt = f"""ORCHESTRATE_PLAN
{schema_hint}

---
Write the plan for this goal in this repository. Read files as needed (you are read-only).

GOAL: {a.goal}

REPO SCAN:
{json.dumps(info, indent=2)[:12000]}

MACHINE: up to {cap['cap']} subagents can run in parallel right now.

Use "repo": "{repo}". Reply with ONLY the plan JSON object."""
    res = claude(prompt, "opus", "high", repo, perm="dontAsk",
                 tools=["Read", "Grep", "Glob", "Bash(git ls-files*)", "Bash(git log*)"])
    plan = extract_json(str(res.get("result", "")))
    for _ in range(2):
        if plan is None:
            errs = ["reply was not a JSON object"]
        else:
            plan["repo"] = str(repo)
            errs, warns, _ = validate(plan)
            if not errs:
                break
        res = claude("The plan is invalid:\n" + "\n".join(errs) + "\nReply with ONLY the corrected plan JSON.",
                     "opus", "high", repo, perm="dontAsk", tools=["Read", "Grep", "Glob"],
                     resume=res.get("session_id"))
        plan = extract_json(str(res.get("result", "")))
    if plan is None:
        print("planner did not return a plan:\n" + str(res.get("result"))[-2000:], file=sys.stderr)
        return 2
    plan["repo"] = str(repo)
    PLANS.mkdir(parents=True, exist_ok=True)
    out = Path(a.out).expanduser() if a.out else PLANS / f"{plan.get('job', 'plan')}.json"
    write_json(out, plan)
    print(out)
    return cmd_validate(argparse.Namespace(plan=str(out), json=False))


def cmd_validate(a) -> int:
    plan = load_plan(Path(a.plan))
    errs, warns, waves = validate(plan)
    cap = capacity(plan)
    if a.json:
        print(json.dumps({"ok": not errs, "errors": errs, "warnings": warns, "waves": waves,
                          "capacity": cap}, indent=2))
        return 0 if not errs else 2
    for e in errs:
        print(f"ERROR   {e}")
    for w in warns:
        print(f"warning {w}")
    if not errs:
        tasks = {t["id"]: t for t in plan["tasks"]}
        print(f"OK: {len(tasks)} tasks in {len(waves)} wave(s); up to {cap['cap']} in parallel "
              f"(limited by {cap['limited_by']})")
        for i, w in enumerate(waves, 1):
            print(f"  wave {i}: " + ", ".join(
                f"{t} [{tasks[t]['type']} {tasks[t]['tier']}"
                + (" GATE" if tasks[t].get("gate") else "") + "]" for t in w))
    return 0 if not errs else 2


def cmd_run(a, resume: bool = False) -> int:
    if resume:
        job = Job(a.job)
        if not job.state:
            raise SystemExit(f"no job {a.job}")
        plan = job.plan
        plan_path = job.dir / "plan.json"
    else:
        plan_path = Path(a.plan).expanduser()
        plan = load_plan(plan_path)
        plan["repo"] = str(Path(plan.get("repo", ".")).expanduser().resolve())
    errs, warns, _ = validate(plan)
    if errs:
        print("plan is invalid:\n" + "\n".join(errs), file=sys.stderr)
        return 2
    if a.detach:
        JOBS.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(Path(__file__).resolve())] + [x for x in sys.argv[1:] if x != "--detach"]
        logp = JOBS / f"{plan['job']}.out"
        with open(logp, "a") as fh:
            p = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True,
                                 cwd=Path.cwd())
        print(json.dumps({"job": plan["job"], "pid": p.pid, "log": str(logp),
                          "next": f"orchestrate.py wait {plan['job']} --timeout 540"}))
        return 0
    job = start_job(plan, plan_path)
    return run_job(job, plan, a.approve or [], a.max_parallel, a.skip_baseline)


def cmd_status(a) -> int:
    job = Job(a.job or latest_job())
    if not job.state:
        raise SystemExit(f"no job {a.job}")
    s = job.state
    alive = pid_alive(s.get("pid"))
    status = s["status"] if not (s["status"] == "running" and not alive) else "stopped (process gone)"
    out = {"job": job.id, "status": status, "cost_usd": round(s.get("cost_usd", 0), 3),
           "branch": s["branch"], "cap": (s.get("capacity") or {}).get("cap"),
           "tasks": {t: {"status": x["status"], "type": x.get("type"), "tier": x.get("tier"),
                         "path": " → ".join(st.get("config", "?") for st in x.get("steps") or [])}
                     for t, x in s["tasks"].items()},
           "message": s.get("message"), "report": str(job.dir / "report.md")}
    print(json.dumps(out, indent=2))
    return 0


def cmd_wait(a) -> int:
    jid = a.job or latest_job()
    end = time.time() + a.timeout
    while True:
        job = Job(jid)
        st = job.state.get("status")
        if not st and time.time() < end:
            time.sleep(2)
            continue
        if st and st != "running":
            print((job.dir / "report.md").read_text() if (job.dir / "report.md").exists() else st)
            return EXIT.get(st, 1)
        if st == "running" and job.state.get("pid") and not pid_alive(job.state["pid"]):
            print(f"job {jid} process is gone; `orchestrate.py resume {jid}` to continue")
            return 1
        if time.time() >= end:
            cmd_status(argparse.Namespace(job=jid))
            print(f"(still running; call `orchestrate.py wait {jid}` again)")
            return 75
        time.sleep(5)


def cmd_clean(a) -> int:
    job = Job(a.job)
    if not job.state:
        raise SystemExit(f"no job {a.job}")
    repo = Path(job.state["repo"])
    if job.state.get("status") == "running" and pid_alive(job.state.get("pid")):
        raise SystemExit("job is still running")
    wt_root = job.dir / "wt"
    if wt_root.exists():
        for d in wt_root.iterdir():
            git(repo, "worktree", "remove", "--force", str(d), check=False)
        shutil.rmtree(wt_root, ignore_errors=True)
    git(repo, "worktree", "prune")
    if a.branches:
        for t in job.state["tasks"]:
            git(repo, "branch", "-D", f"orchestrate/{job.id}--{t}", check=False)
    print(f"cleaned worktrees for {job.id}; integration branch {job.state['branch']} kept")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capacity")
    c.add_argument("--plan")
    s = sub.add_parser("scan"); s.add_argument("--repo", default=".")
    p = sub.add_parser("plan"); p.add_argument("--repo", default="."); p.add_argument("--goal", required=True)
    p.add_argument("--out")
    v = sub.add_parser("validate"); v.add_argument("plan"); v.add_argument("--json", action="store_true")
    b = sub.add_parser("brief"); b.add_argument("plan"); b.add_argument("task")
    for name in ("run", "resume"):
        r = sub.add_parser(name)
        r.add_argument("plan" if name == "run" else "job")
        r.add_argument("--approve", action="append", default=[])
        r.add_argument("--max-parallel", type=int)
        r.add_argument("--detach", action="store_true")
        r.add_argument("--skip-baseline", action="store_true")
    st = sub.add_parser("status"); st.add_argument("job", nargs="?")
    w = sub.add_parser("wait"); w.add_argument("job", nargs="?"); w.add_argument("--timeout", type=int, default=540)
    cl = sub.add_parser("clean"); cl.add_argument("job"); cl.add_argument("--branches", action="store_true")
    sc = sub.add_parser("scope-check"); sc.add_argument("--owns", action="append", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "capacity":
        print(json.dumps(capacity(load_plan(Path(a.plan)) if a.plan else None), indent=2))
    elif a.cmd == "scan":
        print(json.dumps(scan(Path(a.repo).expanduser()), indent=2))
    elif a.cmd == "plan":
        return cmd_plan(a)
    elif a.cmd == "validate":
        return cmd_validate(a)
    elif a.cmd == "brief":
        plan = load_plan(Path(a.plan))
        task = next((t for t in plan["tasks"] if t["id"] == a.task), None)
        if not task:
            raise SystemExit(f"no task {a.task}")
        print(build_brief(plan, task, Path(plan["repo"]).expanduser(), {}))
    elif a.cmd == "run":
        return cmd_run(a)
    elif a.cmd == "resume":
        return cmd_run(a, resume=True)
    elif a.cmd == "status":
        return cmd_status(a)
    elif a.cmd == "wait":
        return cmd_wait(a)
    elif a.cmd == "clean":
        return cmd_clean(a)
    elif a.cmd == "scope-check":
        bad = scope_violations(Path.cwd(), a.owns)
        if bad:
            print("OUT OF SCOPE: you changed files you do not own. Revert these (git checkout -- <f> "
                  "or delete new files) and report the need under open_issues instead:")
            print("\n".join(f"  {b}" for b in bad))
            print("You own: " + ", ".join(a.owns))
            return 1
        print("scope ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
