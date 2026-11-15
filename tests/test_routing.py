#!/usr/bin/env python3
"""End-to-end tests for router.py + run_task.py with a fake `claude` binary.

Run from the repo root: python3 tests/test_routing.py -v
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "orchestrate" / "scripts"
sys.path.insert(0, str(SCRIPTS))

FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, pathlib, sys
    a = sys.argv[1:]
    prompt, model, effort = a[a.index("-p") + 1], a[a.index("--model") + 1], a[a.index("--effort") + 1]
    cfg = json.loads(pathlib.Path(os.environ["FAKE_CFG"]).read_text())
    with open(os.environ["FAKE_CALLS"], "a") as fh:
        fh.write(json.dumps({"model": model, "effort": effort,
                             "classifier": prompt.startswith("FAILURE_CLASSIFIER"),
                             "resume": "--resume" in a, "hint": "Note from a previous" in prompt,
                             "fallback": a[a.index("--fallback-model") + 1] if "--fallback-model" in a else None,
                             "advisor": a[a.index("--advisor") + 1] if "--advisor" in a else None}) + "\\n")
    if prompt.startswith("FAILURE_CLASSIFIER"):
        res = json.dumps({"cause": cfg.get("cause", "capability"), "evidence": "fake evidence",
                          "agents_md": cfg.get("agents_md")})
    else:
        if f"{model}/{effort}" in cfg.get("pass", []):
            pathlib.Path("target.txt").write_text("fixed")
        res = "done"
    ver = cfg.get("ver", "5-5")
    used = "opus" if model == "fable" and cfg.get("no_fable") else model
    print(json.dumps({"result": res, "session_id": "sess-1", "total_cost_usd": 0.01, "is_error": False,
                      "modelUsage": {f"claude-{used}-{ver}": {}}}))
''')


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="routing-test-"))
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "target.txt").write_text("broken")
        subprocess.run("git init -q && git add . && git -c user.email=t@t -c user.name=t commit -qm init",
                       shell=True, cwd=self.repo, check=True)
        fake = self.tmp / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        self.cfg = self.tmp / "cfg.json"
        self.calls = self.tmp / "calls.jsonl"
        self.env = {**os.environ, "ORCHESTRATE_DIR": str(self.state), "ROUTER_CLAUDE_BIN": str(fake),
                    "FAKE_CFG": str(self.cfg), "FAKE_CALLS": str(self.calls), "ROUTER_PROBE_RATE": "0"}
        os.environ.update({k: self.env[k] for k in ("ORCHESTRATE_DIR", "ROUTER_PROBE_RATE")})
        import router
        self.router = importlib.reload(router)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_task(self, ttype, tier, cfg, verify="grep -q fixed target.txt", probe_rate="0", extra=()):
        self.cfg.write_text(json.dumps(cfg))
        (self.repo / "target.txt").write_text("broken")
        self.calls.write_text("")
        env = {**self.env, "ROUTER_PROBE_RATE": probe_rate}
        p = subprocess.run([sys.executable, str(SCRIPTS / "run_task.py"), "--repo", str(self.repo),
                            "--type", ttype, "--tier", tier, "--verify", verify,
                            "--baseline", "test -f target.txt", *extra, "do the thing"],
                           env=env, capture_output=True, text=True)
        summary = json.loads(p.stdout) if p.stdout.strip() else {}
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return p.returncode, summary, calls, p.stderr

    def ledger(self):
        return [json.loads(line) for line in (self.state / "ledger.jsonl").read_text().splitlines()]

    # -------------------------------------------------------------------
    def test_pass_at_default_records_success_and_no_proposal(self):
        code, s, calls, _ = self.run_task("rename:web", "T0", {"pass": ["haiku/medium"]})
        self.assertEqual(code, 0)
        self.assertEqual([(c["model"], c["effort"]) for c in calls], [("haiku", "medium")])  # Haiku default
        self.assertEqual(s["proposals"], [])
        self.assertTrue(self.ledger()[0]["ok"])

    def test_two_attempts_then_capability_escalation_and_raise_proposal(self):
        # below the model's default effort, a failure first restores the effort (Anthropic:
        # "if you lowered effort, restore it; otherwise try the next tier up")
        cfg = {"pass": ["sonnet/high"], "cause": "capability"}
        code, s, calls, err = self.run_task("bugfix:api", "T1", cfg)
        self.assertEqual(code, 0, err)
        workers = [c for c in calls if not c["classifier"]]
        self.assertEqual([(c["model"], c["effort"], c["resume"]) for c in workers],
                         [("sonnet", "medium", False), ("sonnet", "medium", True), ("sonnet", "high", False)])
        self.assertEqual(s["proposals"], [], "one failed task is not enough evidence")

        code, s, *_ = self.run_task("bugfix:api", "T1", cfg)
        self.assertEqual(len(s["proposals"]), 1)
        self.assertEqual(s["proposals"][0]["kind"], "raise_floor")
        # proposal only: routing unchanged until approved
        self.assertEqual(self.router.route("bugfix:api", "T1")["model"], "sonnet")
        self.assertEqual([p["kind"] for p in self.router.pending()], ["raise_floor"])

        self.router.approve(s["proposals"][0]["id"])
        d = self.router.route("bugfix:api", "T1")
        self.assertEqual((d["model"], d["effort"], d["source"]), ("sonnet", "high", "learned"))
        self.assertEqual(self.router.pending(), [])
        self.assertEqual(json.loads((self.state / "routing.json").read_text())["bugfix:api"]["floor_cfg"],
                         "sonnet/high")

        # next task starts at the approved floor
        code, s, calls, _ = self.run_task("bugfix:api", "T1", cfg)
        self.assertEqual((calls[0]["model"], calls[0]["effort"]), ("sonnet", "high"))

    def test_capability_failure_at_default_effort_moves_up_a_model(self):
        code, s, calls, err = self.run_task("bugfix:web", "T1", {"pass": ["opus/medium"], "cause": "capability"})
        self.assertEqual(code, 0, err)
        seen = []
        for c in calls:
            if not c["classifier"] and (c["model"], c["effort"]) not in seen:
                seen.append((c["model"], c["effort"]))
        self.assertEqual(seen, [("sonnet", "medium"), ("sonnet", "high"), ("opus", "medium")])

    def test_t2_runs_low_then_reruns_failures_at_high(self):
        # Anthropic: on Opus 5.5, low then high on failures ~97% at $0.17 vs 95.3% at $0.29 all-high
        code, s, calls, err = self.run_task("feature:api", "T2", {"pass": ["opus/high"], "cause": "effort"})
        self.assertEqual(code, 0, err)
        workers = [(c["model"], c["effort"]) for c in calls if not c["classifier"]]
        self.assertEqual(workers, [("opus", "low"), ("opus", "low"), ("opus", "high")])

    def test_classifier_runs_on_haiku_low(self):
        self.run_task("feature:api", "T2", {"pass": ["opus/high"], "cause": "effort"})
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual({(c["model"], c["effort"]) for c in calls if c["classifier"]}, {("haiku", "low")})

    def test_effort_failure_keeps_model_raises_effort(self):
        code, s, calls, _ = self.run_task("feature:web", "T1", {"pass": ["sonnet/high"], "cause": "effort"})
        self.assertEqual(code, 0)
        workers = [(c["model"], c["effort"]) for c in calls if not c["classifier"]]
        self.assertEqual(workers[-1], ("sonnet", "high"))

    def test_environment_failure_stops_and_never_proposes(self):
        verify = "grep -q fixed target.txt || (echo 'dial tcp 127.0.0.1:5432: connect: connection refused'; false)"
        for _ in range(3):
            code, s, calls, _ = self.run_task("bugfix:api", "T1", {"pass": []}, verify=verify)
            self.assertEqual(code, 3)
            self.assertFalse(any(c["classifier"] for c in calls), "env detected by regex, no LLM call")
        self.assertEqual(self.router.pending(), [])
        self.assertTrue(all(e["cause"] == "environment" for e in self.ledger()))

    def test_baseline_failure_spends_no_tokens(self):
        self.cfg.write_text("{}")
        self.calls.write_text("")
        p = subprocess.run([sys.executable, str(SCRIPTS / "run_task.py"), "--repo", str(self.repo),
                            "--type", "bugfix:api", "--tier", "T1", "--verify", "false", "x"],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(self.calls.read_text(), "")

    def test_context_failure_proposes_agents_md_and_retries_same_model(self):
        cfg = {"pass": [], "cause": "context",
               "agents_md": {"file": "AGENTS.md", "text": "Handlers live in internal/http, not handlers/"}}
        code, s, calls, _ = self.run_task("feature:api", "T1", cfg)
        self.assertEqual(code, 4)
        workers = [c for c in calls if not c["classifier"]]
        self.assertTrue(all(c["model"] == "sonnet" for c in workers), "context never escalates the model")
        self.assertTrue(any(c["hint"] for c in workers))
        props = self.router.pending()
        self.assertEqual([p["kind"] for p in props], ["agents_md"])
        self.assertTrue(all(e["cause"] == "context" for e in self.ledger()))
        self.router.approve(props[0]["id"])
        self.assertIn("Handlers live in internal/http", (self.repo / "AGENTS.md").read_text())

    def test_top_of_ladder_is_fable_after_opus_xhigh(self):
        for _ in range(2):
            code, s, calls, _ = self.run_task("migration:api", "T3", {"pass": [], "cause": "capability"})
            self.assertEqual(code, 4)
        workers = [(c["model"], c["effort"]) for c in calls if not c["classifier"]]
        # T3 starts at Opus's default; Opus goes to xhigh before Fable (Anthropic: "if your
        # evals at xhigh or max still fall short ... move to Claude Fable 5.2")
        self.assertEqual(list(dict.fromkeys(workers)), [("opus", "medium"), ("opus", "xhigh"), ("fable", "high")])
        fable = [c for c in calls if c["model"] == "fable"]
        self.assertTrue(all(c["fallback"] == "opus" for c in fable), "no Fable access -> Opus, not a failure")
        self.assertTrue(all(c["fallback"] is None for c in calls if c["model"] != "fable"))
        kinds = {p["kind"] for p in self.router.pending()}
        self.assertIn("raise_floor", kinds)
        self.assertIn("needs_human", kinds)  # failed at the top

    def test_max_model_caps_the_ladder(self):
        code, s, calls, _ = self.run_task("migration:web", "T3", {"pass": [], "cause": "capability"},
                                          extra=("--max-model", "opus"))
        self.assertEqual(code, 4)
        self.assertNotIn("fable", {c["model"] for c in calls})
        self.assertEqual(s["result"], "gave_up")

    def test_fable_fallback_is_not_evidence(self):
        code, s, calls, _ = self.run_task("migration:mobile", "T3", {"pass": ["fable/high"], "cause": "capability",
                                                                    "no_fable": True})
        fable_steps = [st for st in s["steps"] if st["config"] == "fable/high"]
        self.assertTrue(fable_steps and fable_steps[0]["fell_back_to"].startswith("claude-opus"))
        self.assertFalse(any(e["model"] == "fable" for e in self.ledger()))

    def test_advisor_only_for_valid_pairings(self):
        self.run_task("feature:web", "T1", {"pass": ["sonnet/medium"]}, extra=("--advisor", "fable"))
        self.assertEqual({c["advisor"] for c in [json.loads(x) for x in self.calls.read_text().splitlines()]},
                         {"fable"})
        import models
        self.assertEqual(models.advisor_args("fable", "opus"), [])   # advisor must rank >= main model
        self.assertEqual(models.advisor_args("opus", "opus"), ["--advisor", "opus"])
        self.assertEqual(models.advisor_args("sonnet", None), [])

    def test_floors_from_the_old_ladder_still_route(self):
        # v1 stored floors as indexes into haiku/low..opus/xhigh; 4 was opus/high
        (self.state / "routing.json").write_text(json.dumps({"bugfix:api": {
            "floor": 4, "approved_at": __import__("time").time(), "model_alias": "sonnet"}}))
        d = self.router.route("bugfix:api", "T1")
        self.assertEqual((d["model"], d["effort"], d["source"]), ("opus", "high", "learned"))
        (self.state / "ledger.jsonl").write_text(json.dumps({
            "t": 0, "task_id": "x", "type": "bugfix:api", "tier": "T1", "rung": 4, "model": "opus",
            "effort": "high", "ok": True}) + "\n")
        self.assertEqual(self.router._ledger()[0]["rung"], self.router.LADDER.index(("opus", "high")))

    def test_role_defaults_follow_anthropic_guidance(self):
        import models
        self.assertEqual(models.role("classifier"), ("haiku", "low"))
        self.assertEqual(models.role("manager"), ("opus", "medium"))
        self.assertEqual(models.role("planner"), ("fable", "high"))
        self.assertEqual(models.role("reviewer"), ("fable", "high"))
        self.assertEqual(models.role("reviewer", {"max_model": "opus"}), ("opus", "high"))
        self.assertEqual(models.role("manager", {"models": {"manager": "sonnet/high"}}), ("sonnet", "high"))
        self.assertEqual(models.role("lessons", {"models": {"lessons": "sonnet"}}), ("sonnet", "high"))
        self.assertTrue(models.validate_plan({"models": {"planer": "opus"}}))
        self.assertTrue(models.validate_plan({"models": {"manager": "gpt/high"}}))
        self.assertEqual(models.validate_plan({"models": {"manager": "opus/medium"}, "max_model": "opus"}), [])

    def test_rule_expires_when_model_version_changes(self):
        cfg = {"pass": ["sonnet/high"], "cause": "capability"}
        self.run_task("bugfix:mobile", "T1", cfg)
        _, s, *_ = self.run_task("bugfix:mobile", "T1", cfg)
        self.router.approve(s["proposals"][0]["id"])
        self.assertEqual(self.router.route("bugfix:mobile", "T1")["source"], "learned")
        self.router.note_alias("sonnet", "claude-sonnet-5-6")  # new Sonnet ships
        d = self.router.route("bugfix:mobile", "T1")
        self.assertEqual((d["model"], d["source"]), ("sonnet", "tier-default"))
        self.assertIn("expired", d["rule"])

    def test_probe_wins_propose_lowering_the_floor(self):
        cfg = {"pass": ["opus/medium"], "cause": "capability"}
        self.run_task("refactor:web", "T1", cfg)
        _, s, *_ = self.run_task("refactor:web", "T1", cfg)
        floor = next(p for p in s["proposals"] if "opus/medium" in p["reason"])
        self.router.approve(floor["id"])                        # floor = opus/medium
        props = []
        for _ in range(3):  # probe one rung lower, at opus/low, which now passes
            code, s, calls, _ = self.run_task("refactor:web", "T1", {"pass": ["opus/low"]}, probe_rate="1")
            self.assertEqual(code, 0)
            self.assertEqual((calls[0]["model"], calls[0]["effort"]), ("opus", "low"))
            props += s["proposals"]
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["kind"], "lower_floor")
        self.router.approve(props[0]["id"])
        d = self.router.route("refactor:web", "T1")
        self.assertEqual((d["model"], d["effort"]), ("opus", "low"))

    def test_failed_probe_returns_to_floor(self):
        cfg = {"pass": ["opus/medium"], "cause": "capability"}
        self.run_task("test:api", "T1", cfg)
        _, s, *_ = self.run_task("test:api", "T1", cfg)
        self.router.approve(next(p for p in s["proposals"] if "opus/medium" in p["reason"])["id"])
        code, s, calls, _ = self.run_task("test:api", "T1", cfg, probe_rate="1")
        workers = [(c["model"], c["effort"]) for c in calls if not c["classifier"]]
        self.assertEqual(workers[0], ("opus", "low"))       # the probe
        self.assertEqual(workers[-1], ("opus", "medium"))   # straight back to the floor
        self.assertEqual(code, 0)

    def test_bad_task_type_rejected(self):
        with self.assertRaises(SystemExit):
            self.router.route("stuff:api", "T1")
        with self.assertRaises(SystemExit):
            self.router.route("bugfix:Not A Slug", "T1")
        self.assertEqual(self.router.route("bugfix:billing-service", "T1")["model"], "sonnet")

    def test_default_state_dir_is_outside_the_skill(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ORCHESTRATE_DIR", "XDG_DATA_HOME")}
        env["HOME"] = str(self.tmp / "home")
        out = subprocess.run([sys.executable, "-c", "import router; print(router.DIR)"],
                             cwd=SCRIPTS, env=env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, str(self.tmp / "home/.local/share/orchestrate"))


    def test_test_output_is_not_mistaken_for_a_broken_environment(self):
        import run_task
        rt = importlib.reload(run_task)
        code_failures = ["--- FAIL: TestRateLimit (0.00s)\n    handler_test.go:41: expected status 429, got 200",
                         "expected 529 retry-after header", "Error: Cannot find module 'zod'",
                         "AssertionError: rate limit not applied"]
        for out in code_failures:
            self.assertIsNone(rt.env_signature(out, ["make test-api"]), out)
        infra = {"dial tcp 127.0.0.1:5432: connect: connection refused": ["make test-api"],
                 "make: *** No rule to make target 'test-api'.  Stop.": ["make test-api"],
                 "bash: line 1: pnpm: command not found": ["pnpm --filter web test"],
                 "/bin/sh: 1: go: not found": ["cd apps/api && env GOTMPDIR=x go test ./..."]}
        for out, cmds in infra.items():
            self.assertIsNotNone(rt.env_signature(out, cmds), out)

    def test_claude_errors_map_to_causes(self):
        import run_task
        rt = importlib.reload(run_task)
        self.assertEqual(rt.classify("t", 2, {"is_error": True, "error_kind": "max_turns"}, "", self.repo, 5)["cause"], "effort")
        self.assertEqual(rt.classify("t", 2, {"is_error": True, "error_kind": "timeout"}, "", self.repo, 5)["cause"], "unknown")
        self.assertEqual(rt.classify("t", 2, {"is_error": True, "error_kind": "overloaded"}, "", self.repo, 5)["cause"], "environment")

    def test_api_overload_is_retried_not_blamed(self):
        import claude_cli
        cc = importlib.reload(claude_cli)
        fake = self.tmp / "flaky"
        counter = self.tmp / "n"
        fake.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, pathlib
            p = pathlib.Path({str(counter)!r}); n = int(p.read_text()) if p.exists() else 0; p.write_text(str(n + 1))
            if n < 2:
                print(json.dumps({{"is_error": True, "subtype": "error_during_execution", "api_error_status": 529,
                                  "result": "overloaded_error"}}))
            else:
                print(json.dumps({{"is_error": False, "subtype": "success", "result": "ok", "num_turns": 2,
                                  "structured_output": {{"cause": "effort", "evidence": "e"}},
                                  "usage": {{"input_tokens": 1, "cache_read_input_tokens": 9}}}}))
        """))
        fake.chmod(0o755)
        os.environ["ROUTER_CLAUDE_BIN"], os.environ["ORCHESTRATE_API_BACKOFF_S"] = str(fake), "0"
        try:
            r = cc.call("x", "sonnet", "low", self.repo, perm="plan", tools=[], schema={"type": "object"})
        finally:
            del os.environ["ROUTER_CLAUDE_BIN"], os.environ["ORCHESTRATE_API_BACKOFF_S"]
        self.assertFalse(r["is_error"])
        self.assertEqual(r["structured"], {"cause": "effort", "evidence": "e"})
        self.assertEqual(r["metrics"]["cache_read"], 9)
        self.assertEqual(counter.read_text(), "3")

    def test_budget_stops_the_task(self):
        fake = self.tmp / "claude"
        fake.write_text(FAKE_CLAUDE.replace('print(json.dumps({"result": res,',
                                            'print(json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "result": res,')
                        .replace('"is_error": False,', ""))
        code, s, calls, err = self.run_task("feature:web", "T1", {"pass": []})
        self.assertEqual(code, 7, err)
        self.assertEqual(s["result"], "budget")
        self.assertFalse(any(c["classifier"] for c in calls), "budget is not a failure to classify")

if __name__ == "__main__":
    unittest.main()
