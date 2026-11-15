#!/usr/bin/env python3
"""End-to-end tests for orchestrate.py with a fake `claude` on a pnpm/Go monorepo.

Run from the repo root: python3 tests/test_orchestrate.py -v
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import shutil
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "orchestrate" / "scripts"
ORCH = SCRIPTS / "orchestrate.py"
sys.path.insert(0, str(SCRIPTS))

FAKE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, pathlib, re, subprocess, sys, time
    a = sys.argv[1:]
    prompt, model, effort = a[a.index("-p") + 1], a[a.index("--model") + 1], a[a.index("--effort") + 1]
    cfg = json.loads(pathlib.Path(os.environ["FAKE_CFG"]).read_text())
    calls = pathlib.Path(os.environ["FAKE_CALLS"])
    cwd = pathlib.Path.cwd()
    def out(result):
        print(json.dumps({"result": result, "session_id": "s-" + cwd.name, "total_cost_usd": 0.01,
                          "is_error": False, "modelUsage": {f"claude-{model}-5-5": {}}}))
        sys.exit(0)
    def note(kind, **kw):
        with open(calls, "a") as fh:
            fh.write(json.dumps({"kind": kind, "model": model, "effort": effort, "cwd": cwd.name, **kw}) + "\n")
    if prompt.startswith("FAILURE_CLASSIFIER"):
        note("classifier"); out(json.dumps({"cause": cfg.get("cause", "capability"), "evidence": "fake"}))
    if prompt.startswith("ORCHESTRATE_REVIEW"):
        n = sum(1 for l in calls.read_text().splitlines() if '"review"' in l) if calls.exists() else 0
        note("review")
        pathlib.Path(str(calls) + ".review_prompt").write_text(prompt)
        reviews = cfg.get("reviews", [{"verdict": "pass", "summary": "ok", "findings": []}])
        out(json.dumps(reviews[min(n, len(reviews) - 1)]))
    if prompt.startswith("ORCHESTRATE_MERGE"):
        note("merge")
        files = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True).stdout.split()
        for f in files:
            t = pathlib.Path(f).read_text()
            t = re.sub(r"<<<<<<< [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", r"\1\2", t, flags=re.S)
            pathlib.Path(f).write_text(t)
            subprocess.run(["git", "add", f])
        out('```json\n{"resolved": true}\n```')
    if prompt.startswith("ORCHESTRATE_PLAN"):
        note("plan"); out(json.dumps(cfg["plan"]))
    if prompt.startswith("ORCHESTRATE_MANAGE"):
        n = sum(1 for l in calls.read_text().splitlines() if '"kind": "manage"' in l) if calls.exists() else 0
        ev = re.search(r"EVENT: (\w+)", prompt).group(1)
        note("manage", event=ev, resumed="--resume" in a, has_playbook="# Managing an orchestrate job" in prompt)
        pathlib.Path(str(calls) + f".manage{n}").write_text(prompt)
        resp = cfg.get("manager", [])
        out(json.dumps(resp[n] if n < len(resp) else {"note": "noop", "actions": [{"action": "none"}]}))
    # ---- worker ----
    tid = cwd.name
    t = cfg.get("tasks", {}).get(tid, {})
    retry = prompt.startswith("Verification still fails")
    start = time.time()
    time.sleep(t.get("sleep", 0))
    if tid == "_integration":
        for p, c in cfg.get("fix", {}).items():
            pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True); pathlib.Path(p).write_text(c)
    elif t.get("fail_until_note") and "Notes from the manager" not in prompt:
        pass
    elif not t.get("never"):
        if retry and t.get("out_of_scope_first"):
            pathlib.Path(t["out_of_scope_first"]).unlink(missing_ok=True)
        if not (t.get("fail_first") and not retry):
            for p, c in t.get("write", {}).items():
                pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True); pathlib.Path(p).write_text(c)
        if t.get("out_of_scope_first") and not retry:
            pathlib.Path(t["out_of_scope_first"]).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(t["out_of_scope_first"]).write_text("oops")
    note("worker", tid=tid, retry=retry, start=start, end=time.time(),
         brief_has_upstream="Already done by upstream" in prompt, prompt_head=prompt[:120])
    out(f"did {tid}\n```json\n" + json.dumps({"summary": f"did {tid}", "interfaces": f"{tid}-api",
        "notes_for_dependents": f"use {tid}-api", "open_issues": t.get("open_issues", [])}) + "\n```")
''').lstrip()


def make_monorepo(root: Path) -> None:
    files = {
        "AGENTS.md": "# root rules\n",
        "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n  - 'packages/*'\n",
        "package.json": json.dumps({"name": "mono", "private": True, "scripts": {"test": "make test"}}),
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
        "Makefile": "lint: ## lint all\n\ttrue\ntest-api: ## go test\n\ttrue\ntest-web: ## web\n\ttrue\n",
        "apps/api/go.mod": "module example.com/api\n\ngo 1.22\n",
        "apps/api/main.go": "package main\n",
        "apps/api/AGENTS.md": "# api rules\n",
        "apps/web/package.json": json.dumps({"name": "web", "scripts": {"test": "vitest", "typecheck": "tsc", "dev": "next"}}),
        "apps/web/src/index.ts": "export {}\n",
        "packages/contract/package.json": json.dumps({"name": "@app/contract", "scripts": {"test": "vitest"}}),
        "packages/contract/src/index.ts": "export const v = 1\n",
        ".agents/vendored/tool/package.json": json.dumps({"name": "vendored"}),
        ".agents/vendored/tool/bun.lock": "{}",
        "apps/web/fixtures/demo/package.json": json.dumps({"name": "fixture"}),
    }
    for p, c in files.items():
        f = root / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    subprocess.run("git init -q -b main && git add -A && git -c user.email=t@t -c user.name=t commit -qm init",
                   shell=True, cwd=root, check=True)


def base_plan(repo: Path, **over) -> dict:
    plan = {
        "job": "share", "goal": "share links", "repo": str(repo),
        "acceptance": ["links work"], "setup": [],
        "verify": ["make lint"], "max_parallel": 3,
        "tasks": [
            {"id": "contract", "title": "types", "type": "feature:contract", "tier": "T1",
             "goal": "add share types", "owns": ["packages/contract/src/share/**"],
             "done": ["types exist"], "verify": ["test -f packages/contract/src/share/types.ts"]},
            {"id": "api", "title": "endpoints", "type": "feature:api", "tier": "T2",
             "goal": "share endpoints", "owns": ["apps/api/share/"], "depends_on": ["contract"],
             "done": ["endpoint exists"], "verify": ["test -f apps/api/share/handler.go"]},
            {"id": "web", "title": "button", "type": "feature:web", "tier": "T1",
             "goal": "share button", "owns": ["apps/web/src/share/**"], "depends_on": ["contract"],
             "done": ["button exists"], "verify": ["test -f apps/web/src/share/Button.tsx"]},
        ],
    }
    plan.update(over)
    return plan


WRITES = {
    "contract": {"write": {"packages/contract/src/share/types.ts": "export type S = {}\n"}},
    "api": {"write": {"apps/api/share/handler.go": "package share\n"}},
    "web": {"write": {"apps/web/src/share/Button.tsx": "export const B = 1\n"}},
}


class OrchestrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="orch-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        make_monorepo(self.repo)
        self.state = self.tmp / "state"
        fake = self.tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.cfg = self.tmp / "cfg.json"
        self.calls = self.tmp / "calls.jsonl"
        self.env = {**os.environ, "ORCHESTRATE_DIR": str(self.state), "ROUTER_CLAUDE_BIN": str(fake),
                    "FAKE_CFG": str(self.cfg), "FAKE_CALLS": str(self.calls), "ROUTER_PROBE_RATE": "0",
                    "ORCHESTRATE_MEM_PER_AGENT_GB": "0.01", "ORCHESTRATE_CPU_PER_AGENT": "0.01"}
        os.environ.update({k: self.env[k] for k in ("ORCHESTRATE_DIR", "ROUTER_PROBE_RATE",
                                                    "ROUTER_CLAUDE_BIN", "FAKE_CFG", "FAKE_CALLS")})
        import router
        import orchestrate
        self.router = importlib.reload(router)
        self.o = importlib.reload(orchestrate)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    def write_plan(self, plan) -> Path:
        p = self.tmp / "plan.json"
        p.write_text(json.dumps(plan))
        return p

    def cli(self, *args, cfg=None, timeout=120):
        if cfg is not None:
            self.cfg.write_text(json.dumps(cfg))
        p = subprocess.run([sys.executable, str(ORCH), *args], env=self.env, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def calls_of(self, kind):
        if not self.calls.exists():
            return []
        return [json.loads(l) for l in self.calls.read_text().splitlines() if f'"kind": "{kind}"' in l]

    def branch_files(self, job="share"):
        return subprocess.run(["git", "ls-tree", "-r", "--name-only", f"orchestrate/{job}"], cwd=self.repo,
                              capture_output=True, text=True).stdout.split()

    def job_state(self, job="share"):
        return json.loads((self.state / "jobs" / job / "job.json").read_text())

    # -------------------------------------------------------------- tests
    def test_scan_understands_the_monorepo(self):
        info = self.o.scan(self.repo)
        self.assertTrue(info["monorepo"])
        areas = {p["area"]: p for p in info["packages"]}
        self.assertEqual(set(areas), {"api", "web", "contract"})
        self.assertEqual(areas["api"]["verify_suggestions"], ["make test-api"])
        self.assertEqual(areas["web"]["verify_suggestions"], ["make test-web"])
        self.assertIn("pnpm --filter @app/contract run test", areas["contract"]["verify_suggestions"])
        self.assertEqual(info["suggested_setup"], ["pnpm install --frozen-lockfile --prefer-offline"])
        for h in ("pnpm-lock.yaml", "package.json", "Makefile", "pnpm-workspace.yaml"):
            self.assertIn(h, info["hotspots"])
        self.assertIn("apps/api/AGENTS.md", info["agents_docs"])
        self.assertNotIn(".agents/vendored/tool/bun.lock", info["hotspots"])  # vendored/hidden skipped

    def test_ownership_overlap_rules(self):
        ov = lambda a, b: self.o.patterns_overlap(a, b, ["apps/api/main.go", "apps/web/src/index.ts"])
        self.assertTrue(ov("apps/**", "apps/api/x.go"))
        self.assertTrue(ov("apps/api/", "apps/api/internal/**"))
        self.assertTrue(ov("apps/api/**/*.go", "apps/api/internal/**"))
        self.assertTrue(ov("**/*.ts", "apps/web/src/**"))
        self.assertFalse(ov("apps/api/*.go", "apps/api/*.ts"))
        self.assertFalse(ov("apps/web/**", "apps/web-admin/**"))
        self.assertFalse(ov("apps/api/", "apps/web/"))
        self.assertTrue(self.o.owned("apps/api/share/h.go", "apps/api/share/"))
        self.assertFalse(self.o.owned("apps/api/x/h.go", "apps/api/*.go"))

    def test_validate_catches_bad_plans_and_prints_waves(self):
        good = base_plan(self.repo)
        errs, warns, waves = self.o.validate(good)
        self.assertEqual(errs, [])
        self.assertEqual(waves, [["contract"], ["api", "web"]])

        bad = base_plan(self.repo)
        bad["tasks"][2]["owns"] = ["apps/**"]  # web overlaps api, and they run in parallel
        errs, _, _ = self.o.validate(bad)
        self.assertTrue(any("both own" in e for e in errs), errs)

        ordered = base_plan(self.repo)
        ordered["tasks"][2]["owns"] = ["apps/**"]
        ordered["tasks"][2]["depends_on"] = ["contract", "api"]
        errs, warns, _ = self.o.validate(ordered)
        self.assertEqual(errs, [])
        self.assertTrue(any("run in order" in w for w in warns))

        cyc = base_plan(self.repo)
        cyc["tasks"][0]["depends_on"] = ["api"]
        self.assertTrue(any("cycle" in e for e in self.o.validate(cyc)[0]))

        self.assertTrue(any("decisions must be a list of strings" in e
                            for e in self.o.validate(base_plan(self.repo, decisions="use jwt"))[0]))

        missing = base_plan(self.repo)
        del missing["tasks"][1]["verify"]
        missing["tasks"][0]["type"] = "stuff:contract"
        errs = self.o.validate(missing)[0]
        self.assertTrue(any("verify is required" in e for e in errs))
        self.assertTrue(any("bad task type" in e for e in errs))

    def test_capacity_math(self):
        cap = self.o.compute_cap(1.7, 8, 10.6, 1.5, 1.0, 6)   # this dev box under load
        self.assertEqual((cap["cap"], cap["limited_by"]), (1, "memory"))
        cap = self.o.compute_cap(16, 8, 0.5, 1.5, 1.0, 6)
        self.assertEqual((cap["cap"], cap["limited_by"]), (6, "hard_max"))
        cap = self.o.compute_cap(16, 4, 1.0, 1.5, 1.0, 6)
        self.assertEqual((cap["cap"], cap["limited_by"]), (3, "cpu"))
        self.assertEqual(self.o.compute_cap(64, 32, 0, 1.5, 1.0, 6, plan_max=2)["cap"], 2)
        live = self.o.capacity()
        self.assertGreaterEqual(live["cap"], 1)
        self.assertIn("mem_available_gb", live)

    def test_brief_is_a_complete_handoff(self):
        (self.repo / "CONTEXT.md").write_text("# Glossary\n**Share link**: a read-only URL\n")
        plan = base_plan(self.repo, decisions=["Tokens are opaque random strings (docs/adr/0007.md)"],
                         non_goals=["No expiry UI"])
        upstream = {"contract": {"title": "types", "files": ["packages/contract/src/share/types.ts"],
                                 "report": {"summary": "added S", "interfaces": "type S",
                                            "notes_for_dependents": "import S from contract"}}}
        b = self.o.build_brief(plan, plan["tasks"][1], self.repo, upstream)
        for needle in ("## Your goal", "share endpoints", "`apps/api/share/`", "`AGENTS.md`",
                       "`apps/api/AGENTS.md`", "added S", "import S from contract",
                       "## Done looks like", "endpoint exists", "`test -f apps/api/share/handler.go`",
                       "Do not commit", "```json",
                       "Decisions already made with the user", "Tokens are opaque random strings",
                       "Out of scope for this whole job", "No expiry UI", "`CONTEXT.md`"):
            self.assertIn(needle, b)
        self.assertLess(b.index("`CONTEXT.md`"), b.index("`apps/api/AGENTS.md`"), "glossary read first")

    def test_scope_check_handles_modified_tracked_files(self):
        # regression: ` M path` as the FIRST porcelain line used to lose its leading space
        (self.repo / "packages/contract/src/index.ts").write_text("export const v = 2\n")
        (self.repo / "packages/contract/src/new.ts").write_text("x\n")
        self.assertEqual(self.o.scope_violations(self.repo, ["packages/contract/src/"]), [])
        (self.repo / "apps/api/main.go").write_text("package main // edited\n")
        self.assertEqual(self.o.scope_violations(self.repo, ["packages/contract/src/"]), ["apps/api/main.go"])

    def test_guard_detects_tampering_with_skill_files(self):
        fake_skill = self.tmp / "skills" / "orchestrate" / "scripts"
        shutil.copytree(SCRIPTS, fake_skill)
        old = self.o.HERE
        try:
            self.o.HERE = fake_skill
            before = self.o.guard_snapshot()
            self.assertEqual(before, self.o.guard_snapshot())
            (fake_skill / "orchestrate.py").write_text("# tampered\n")
            self.assertNotEqual(before, self.o.guard_snapshot())
        finally:
            self.o.HERE = old

    def test_full_job_parallel_merge_review(self):
        cfg = {"tasks": {k: {**v, "sleep": 1.5} for k, v in WRITES.items()}}
        code, out, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        files = self.branch_files()
        for f in ("packages/contract/src/share/types.ts", "apps/api/share/handler.go",
                  "apps/web/src/share/Button.tsx"):
            self.assertIn(f, files)
        w = {c["tid"]: c for c in self.calls_of("worker")}
        self.assertLess(w["contract"]["end"], min(w["api"]["start"], w["web"]["start"]), "contract first")
        self.assertLess(max(w["api"]["start"], w["web"]["start"]), min(w["api"]["end"], w["web"]["end"]),
                        "api and web ran in parallel")
        self.assertTrue(w["api"]["brief_has_upstream"])
        self.assertEqual([(c["model"], c["effort"]) for c in self.calls_of("review")], [("opus", "high")])
        st = self.job_state()
        self.assertEqual(st["status"], "done")
        self.assertEqual({t: s["status"] for t, s in st["tasks"].items()},
                         {"contract": "done", "api": "done", "web": "done"})
        self.assertEqual(st["tasks"]["api"]["steps"][0]["config"], "sonnet/high")   # T2 routing
        self.assertEqual(st["tasks"]["web"]["steps"][0]["config"], "sonnet/medium") # T1 routing
        wt = self.state / "jobs" / "share" / "wt"
        self.assertEqual(sorted(p.name for p in wt.iterdir()), ["_integration"], "task worktrees cleaned")
        self.assertIn("DONE", out)
        self.assertIn("git -C", out)
        # user's branch untouched
        self.assertEqual(subprocess.run(["git", "log", "--oneline", "main"], cwd=self.repo,
                                        capture_output=True, text=True).stdout.count("\n"), 1)

    def test_cap_one_runs_serially(self):
        cfg = {"tasks": {k: {**v, "sleep": 0.5} for k, v in WRITES.items()}}
        code, _, err = self.cli("run", str(self.write_plan(base_plan(self.repo, max_parallel=1))), cfg=cfg)
        self.assertEqual(code, 0, err[-2000:])
        spans = sorted((c["start"], c["end"]) for c in self.calls_of("worker"))
        for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
            self.assertLessEqual(e1, s2 + 0.05)

    def test_out_of_scope_edit_is_caught_and_fixed(self):
        cfg = {"tasks": {**WRITES, "web": {**WRITES["web"], "out_of_scope_first": "apps/api/share/evil.go"}}}
        code, _, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        web = [c for c in self.calls_of("worker") if c["tid"] == "web"]
        self.assertEqual([c["retry"] for c in web], [False, True])
        self.assertNotIn("apps/api/share/evil.go", self.branch_files())

    def test_failed_task_blocks_dependents_and_skips_review(self):
        plan = base_plan(self.repo)
        plan["tasks"].append({"id": "docs", "title": "docs", "type": "docs:api", "tier": "T0",
                              "goal": "doc it", "owns": ["docs/share.md"], "depends_on": ["api"],
                              "done": ["doc"], "verify": ["test -f docs/share.md"]})
        cfg = {"tasks": {**WRITES, "api": {"never": True}}, "cause": "capability"}
        code, out, err = self.cli("run", str(self.write_plan(plan)), "--max-parallel", "2", cfg=cfg)
        self.assertEqual(code, 4, err[-3000:])
        st = self.job_state()
        self.assertEqual(st["tasks"]["api"]["status"], "failed")
        self.assertEqual(st["tasks"]["docs"]["status"], "blocked")
        self.assertEqual(st["tasks"]["web"]["status"], "done")
        self.assertEqual(self.calls_of("review"), [])
        api_cfgs = [c["model"] for c in self.calls_of("worker") if c["tid"] == "api"]
        self.assertIn("opus", api_cfgs, "escalated before giving up")

    def test_gate_pauses_then_resume_with_approval(self):
        plan = base_plan(self.repo)
        plan["tasks"][1]["gate"] = "creates a migration"
        code, out, err = self.cli("run", str(self.write_plan(plan)), cfg={"tasks": WRITES})
        self.assertEqual(code, 5, err[-3000:])
        st = self.job_state()
        self.assertEqual(st["tasks"]["api"]["status"], "awaiting_approval")
        self.assertEqual(st["tasks"]["web"]["status"], "done")
        self.assertIn("--approve api", st["message"])
        n_contract = len([c for c in self.calls_of("worker") if c["tid"] == "contract"])
        code, out, err = self.cli("resume", "share", "--approve", "api")
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual(len([c for c in self.calls_of("worker") if c["tid"] == "contract"]), n_contract,
                         "done tasks are not re-run")
        self.assertIn("apps/api/share/handler.go", self.branch_files())

    def test_reviewer_gets_decisions_and_non_goals(self):
        plan = base_plan(self.repo, decisions=["Use opaque tokens"], non_goals=["No expiry UI"])
        code, _, err = self.cli("run", str(self.write_plan(plan)), cfg={"tasks": WRITES})
        self.assertEqual(code, 0, err[-2000:])
        heads = [c for c in self.calls_of("review")]
        self.assertEqual(len(heads), 1)
        review_prompt = Path(str(self.calls) + ".review_prompt").read_text()
        self.assertIn("Use opaque tokens", review_prompt)
        self.assertIn("No expiry UI", review_prompt)
        brief = (self.state / "jobs/share/briefs/api.md").read_text()
        self.assertIn("Use opaque tokens", brief)

    # ------------------------------------------------------------ manager
    def test_manager_retries_failed_task_with_instructions(self):
        cfg = {"tasks": {**WRITES, "api": {**WRITES["api"], "fail_until_note": True}},
               "manager": [{"note": "it never created the handler", "actions": [
                   {"action": "retry", "task": "api", "instructions": "Create apps/api/share/handler.go",
                    "reads": ["apps/api/main.go"]}]}]}
        code, out, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        m = self.calls_of("manage")
        self.assertEqual([c["event"] for c in m], ["task_failed"])
        self.assertEqual((m[0]["model"], m[0]["effort"]), ("opus", "high"))
        self.assertTrue(m[0]["has_playbook"])
        brief = (self.state / "jobs/share/briefs/api.md").read_text()
        self.assertIn("Notes from the manager", brief)
        self.assertIn("Create apps/api/share/handler.go", brief)
        self.assertIn("`apps/api/main.go`", brief)
        st = self.job_state()
        self.assertEqual(st["tasks"]["api"]["manager_retries"], 1)
        self.assertEqual(st["status"], "done")
        self.assertIn("Manager interventions (1)", out)
        self.assertIn("apps/api/share/handler.go", self.branch_files())

    def test_manager_session_persists_across_events(self):
        cfg = {"tasks": {**WRITES, "api": {**WRITES["api"], "fail_until_note": True},
                         "web": {**WRITES["web"], "open_issues": ["nav entry lives in apps/web/src/nav.ts"]}},
               "manager": [{"actions": [{"action": "retry", "task": "api", "instructions": "write it"}]},
                           {"note": "nav is a non-goal", "actions": [{"action": "none"}]}]}
        code, _, err = self.cli("run", str(self.write_plan(base_plan(self.repo, max_parallel=1))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        m = self.calls_of("manage")
        self.assertEqual([c["event"] for c in m], ["task_failed", "open_issues"])
        self.assertEqual([c["resumed"] for c in m], [False, True])
        self.assertEqual([c["has_playbook"] for c in m], [True, False], "playbook only in the first prompt")

    def test_manager_splits_a_failing_task_and_rewires_dependents(self):
        plan = base_plan(self.repo)
        plan["tasks"].append({"id": "docs", "title": "docs", "type": "docs:api", "tier": "T0", "goal": "doc",
                              "owns": ["docs/share.md"], "depends_on": ["api"], "done": ["doc"],
                              "verify": ["test -f docs/share.md"]})
        part = lambda i, f: {"id": i, "title": i, "goal": i, "type": "feature:api", "tier": "T1",
                             "owns": [f], "done": ["exists"], "verify": [f"test -f {f}"]}
        cfg = {"tasks": {**WRITES, "api": {"never": True},
                         "api-model": {"write": {"apps/api/share/model.go": "package share\n"}},
                         "api-handler": {"write": {"apps/api/share/handler.go": "package share\n"}},
                         "docs": {"write": {"docs/share.md": "# share\n"}}},
               "manager": [{"note": "too big; split", "actions": [{"action": "split", "task": "api", "into": [
                   part("api-model", "apps/api/share/model.go"),
                   {**part("api-handler", "apps/api/share/handler.go"), "depends_on": ["contract", "api-model"]}]}]}]}
        code, out, err = self.cli("run", str(self.write_plan(plan)), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        saved = json.loads((self.state / "jobs/share/plan.json").read_text())
        ids = {t["id"]: t for t in saved["tasks"]}
        self.assertNotIn("api", ids)
        self.assertEqual(ids["api-model"]["depends_on"], ["contract"], "inherits the split task's deps")
        self.assertEqual(sorted(ids["docs"]["depends_on"]), ["api-handler", "api-model"])
        st = self.job_state()
        self.assertEqual(st["tasks"]["api"]["status"], "split")
        for f in ("apps/api/share/model.go", "apps/api/share/handler.go", "docs/share.md"):
            self.assertIn(f, self.branch_files())
        self.assertIn("split into api-model, api-handler", out)

    def test_manager_turns_open_issue_into_a_task(self):
        cfg = {"tasks": {**WRITES, "web": {**WRITES["web"], "open_issues": ["route must be registered in apps/web/src/routes.ts"]},
                         "web-route": {"write": {"apps/web/src/routes.ts": "export const r = ['share']\n"}}},
               "manager": [{"actions": [{"action": "add_task", "task": {
                   "id": "web-route", "title": "register route", "goal": "register the share route",
                   "type": "feature:web", "tier": "T0", "owns": ["apps/web/src/routes.ts"], "depends_on": ["web"],
                   "done": ["route registered"], "verify": ["test -f apps/web/src/routes.ts"]}}]}]}
        code, _, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual([c["event"] for c in self.calls_of("manage")], ["open_issues"])
        self.assertIn("apps/web/src/routes.ts", self.branch_files())
        self.assertEqual(self.job_state()["tasks"]["web-route"]["added_by"], "manager")

    def test_manager_fixes_review_findings_with_targeted_tasks(self):
        cfg = {"tasks": {**WRITES, "fix-web-export": {"write": {"apps/web/src/share/index.ts": "export * from './Button'\n"}}},
               "reviews": [{"verdict": "changes_requested", "summary": "not exported",
                            "findings": [{"severity": "major", "file": "apps/web/src/share/Button.tsx",
                                          "issue": "not exported from an index"}]},
                           {"verdict": "pass", "summary": "good", "findings": []}],
               "manager": [{"actions": [{"action": "add_task", "task": {
                   "id": "fix-web-export", "title": "export share", "goal": "add index.ts exporting Button",
                   "type": "bugfix:web", "tier": "T0", "owns": ["apps/web/src/share/index.ts"],
                   "depends_on": ["web"], "done": ["exported"], "verify": ["test -f apps/web/src/share/index.ts"]}}]}]}
        code, _, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual([c["event"] for c in self.calls_of("manage")], ["review_changes_requested"])
        self.assertEqual(len(self.calls_of("review")), 2)
        self.assertFalse([c for c in self.calls_of("worker") if c["tid"] == "_integration"], "no generic fix round")
        self.assertIn("apps/web/src/share/index.ts", self.branch_files())

    def test_manager_asks_human_then_resume_with_answer(self):
        cfg = {"tasks": {**WRITES, "api": {**WRITES["api"], "fail_until_note": True}},
               "manager": [{"actions": [{"action": "ask_human",
                                         "question": "Opaque tokens or JWT? (recommend opaque)"}]},
                           {"actions": [{"action": "retry", "task": "api", "instructions": "Use opaque tokens"}]}]}
        code, out, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 8, err[-3000:])
        self.assertEqual(self.job_state()["status"], "awaiting_human")
        self.assertIn("The manager is asking you:** Opaque tokens or JWT?", out)
        code, out, _ = self.cli("resume", "share")
        self.assertEqual(code, 8)
        self.assertIn("still waiting for your answer", out)
        code, out, err = self.cli("resume", "share", "--answer", "opaque")
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual([c["event"] for c in self.calls_of("manage")], ["task_failed", "human_answer"])
        self.assertIn('"answer": "opaque"', Path(str(self.calls) + ".manage1").read_text())
        self.assertIn("Use opaque tokens", (self.state / "jobs/share/briefs/api.md").read_text())

    def test_invalid_manager_actions_are_rejected_then_corrected(self):
        bad = {"id": "web-extra", "title": "x", "goal": "x", "type": "feature:web", "tier": "T0",
               "owns": ["apps/web/src/share/**"], "done": ["x"], "verify": ["true"]}   # overlaps web, unordered
        good = {**bad, "owns": ["apps/web/src/share/extra.ts"], "depends_on": ["web"],
                "verify": ["test -f apps/web/src/share/extra.ts"]}
        cfg = {"tasks": {**WRITES, "web": {**WRITES["web"], "open_issues": ["needs extra.ts"]},
                         "web-extra": {"write": {"apps/web/src/share/extra.ts": "x\n"}}},
               "manager": [{"actions": [{"action": "add_task", "task": bad}]},
                           {"actions": [{"action": "add_task", "task": good}]}]}
        code, _, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual([c["event"] for c in self.calls_of("manage")], ["open_issues", "rejected_actions"])
        entry = self.job_state()["manager"]["log"][0]
        self.assertTrue(any("both own" in e for e in entry["rejected"]))
        self.assertIn("apps/web/src/share/extra.ts", self.branch_files())

    def test_manager_limits_escalate_to_human(self):
        cfg = {"tasks": {**WRITES, "api": {"never": True}},
               "manager": [{"actions": [{"action": "retry", "task": "api", "instructions": "try again"}]}]}
        plan = base_plan(self.repo, manager={"max_interventions": 1})
        code, out, err = self.cli("run", str(self.write_plan(plan)), cfg=cfg)
        self.assertEqual(code, 8, err[-3000:])
        self.assertEqual(len(self.calls_of("manage")), 1, "the limit stops further manager calls")
        self.assertIn("limit of 1 interventions", self.job_state()["human_question"])

    def test_manager_disabled_keeps_legacy_behaviour(self):
        plan = base_plan(self.repo, manager={"enabled": False})
        code, _, _ = self.cli("run", str(self.write_plan(plan)), cfg={"tasks": {**WRITES, "api": {"never": True}}})
        self.assertEqual(code, 4)
        self.assertEqual(self.calls_of("manage"), [])

    def test_apply_actions_guardrails(self):
        plan = base_plan(self.repo)
        plan["tasks"][1]["gate"] = "creates a migration"
        job = self.o.start_job(plan, self.write_plan(plan))
        job.state["tasks"]["contract"]["status"] = "done"
        job.state["tasks"]["api"]["status"] = "failed"
        job.state["tasks"]["web"]["status"] = "running"
        part = lambda i, f: {"id": i, "title": i, "goal": i, "type": "feature:api", "tier": "T1",
                             "owns": [f], "done": ["x"], "verify": ["true"]}
        for bad, needle in (({"action": "amend", "task": "web", "note": "x"}, "not started"),
                            ({"action": "skip", "task": "contract"}, "cannot skip"),
                            ({"action": "accept"}, "only valid for a review"),
                            ({"action": "retry", "task": "contract"}, "can be retried"),
                            ({"action": "teleport"}, "unknown action")):
            res = self.o.apply_actions(job, plan, {"actions": [bad, {"action": "ask_human", "question": "q"}]}, {"kind": "x"})
            self.assertTrue(any(needle in e for e in res["errors"]), (needle, res))
            self.assertIsNone(res["hint"], "all-or-nothing: the valid action in the batch is not applied")
        res = self.o.apply_actions(job, plan, {"actions": [{"action": "split", "task": "api", "into": [
            part("api-a", "apps/api/share/a.go"), part("api-b", "apps/api/share/b.go")]}]}, {"kind": "task_failed"})
        self.assertEqual(res.get("errors"), None)
        gates = {t["id"]: t.get("gate") for t in plan["tasks"]}
        self.assertEqual(gates["api-a"], "creates a migration", "pieces of a gated task stay gated")
        review = {"kind": "review_changes_requested", "findings": [{"severity": "blocker", "issue": "x"}]}
        res = self.o.apply_actions(job, plan, {"actions": [{"action": "accept", "reason": "meh"}]}, review)
        self.assertTrue(any("blocker" in e for e in res["errors"]))

    def test_review_changes_requested_triggers_fix_round(self):
        cfg = {"tasks": WRITES,
               "reviews": [{"verdict": "changes_requested", "summary": "missing wiring",
                            "findings": [{"severity": "major", "file": "apps/web/src/share/Button.tsx",
                                          "issue": "not exported", "suggestion": "add index"}]},
                           {"verdict": "pass", "summary": "good", "findings": []}],
               "fix": {"apps/web/src/share/index.ts": "export * from './Button'\n"}}
        code, out, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), cfg=cfg)
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual(len(self.calls_of("review")), 2)
        self.assertIn("apps/web/src/share/index.ts", self.branch_files())
        rounds = self.job_state()["review"]["rounds"]
        self.assertEqual([r["verdict"]["verdict"] for r in rounds], ["changes_requested", "pass"])

    def test_review_never_passing_ends_review_failed(self):
        bad = {"verdict": "changes_requested", "summary": "no",
               "findings": [{"severity": "blocker", "issue": "broken"}]}
        code, out, err = self.cli("run", str(self.write_plan(base_plan(self.repo, review_rounds=2))),
                                  cfg={"tasks": WRITES, "reviews": [bad], "fix": {}})
        self.assertEqual(code, 6, err[-3000:])
        self.assertEqual(self.job_state()["status"], "review_failed")

    def test_broken_baseline_stops_before_any_agent(self):
        plan = base_plan(self.repo, verify=["false"])
        code, out, err = self.cli("run", str(self.write_plan(plan)), cfg={"tasks": WRITES})
        self.assertEqual(code, 3)
        self.assertEqual(self.calls_of("worker"), [])
        self.assertIn("baseline", self.job_state()["message"])

    def test_detach_and_wait(self):
        code, out, err = self.cli("run", str(self.write_plan(base_plan(self.repo))), "--detach",
                                  cfg={"tasks": WRITES})
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["job"], "share")
        code, out, err = self.cli("wait", "share", "--timeout", "90")
        self.assertEqual(code, 0, out + err)
        self.assertIn("DONE", out)
        code, out, _ = self.cli("status", "share")
        self.assertEqual(json.loads(out)["status"], "done")
        code, out, _ = self.cli("clean", "share", "--branches")
        self.assertEqual(code, 0)
        self.assertFalse((self.state / "jobs/share/wt").exists())

    def test_merge_conflict_goes_to_integrator(self):
        plan = base_plan(self.repo)
        self.cfg.write_text(json.dumps({}))
        job = self.o.start_job(plan, self.write_plan(plan))
        integ = self.o.ensure_integration(job)
        # integration branch changed a shared line after the task started
        start = self.o.git(integ, "rev-parse", "HEAD")
        (integ / "AGENTS.md").write_text("# root rules\nfrom-integration\n")
        self.o.git(integ, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "integ change")
        wt, br = self.o.prepare_worktree(job, "web", start)
        (wt / "AGENTS.md").write_text("# root rules\nfrom-web\n")
        job.state["tasks"]["web"]["start_sha"] = start
        ok, info = self.o.commit_and_merge(job, plan, plan["tasks"][2], {"wt": str(wt), "branch": br})
        self.assertTrue(ok, info)
        merged = (integ / "AGENTS.md").read_text()
        self.assertIn("from-integration", merged)
        self.assertIn("from-web", merged)
        self.assertNotIn("<<<<<<<", merged)
        self.assertEqual(len(self.calls_of("merge")), 1)

    def test_headless_planner_writes_valid_plan(self):
        cfg = {"plan": {**base_plan(self.repo), "repo": "/wrong/path"}}
        code, out, err = self.cli("plan", "--repo", str(self.repo), "--goal", "share links", cfg=cfg)
        self.assertEqual(code, 0, out + err)
        path = Path(out.splitlines()[0])
        self.assertEqual(json.loads(path.read_text())["repo"], str(self.repo.resolve()))
        self.assertIn("2 wave(s)", out)


if __name__ == "__main__":
    unittest.main()
