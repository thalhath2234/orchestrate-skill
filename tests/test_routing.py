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
                             "advisor": a[a.index("--advisor") + 1] if "--advisor" in a else None,
                             "consult": "consult the advisor" in prompt.lower()}) + "\\n")
    if "--advisor" in a and cfg.get("advisor_launch_fail"):
        sys.stderr.write("Error: Fable as the advisor needs your consent to bill usage credits. Run /model fable.\\n")
        sys.exit(1)
    if prompt.startswith("FAILURE_CLASSIFIER"):
        res = json.dumps({"cause": cfg.get("cause", "capability"), "evidence": "fake evidence",
                          "agents_md": cfg.get("agents_md")})
    else:
        if f"{model}/{effort}" in cfg.get("pass", []):
            pathlib.Path("target.txt").write_text("fixed")
        res = "done"
    if cfg.get("refuse") and not prompt.startswith("FAILURE_CLASSIFIER"):
        print(json.dumps({"result": "", "session_id": "sess-1", "total_cost_usd": 0.001, "is_error": False,
                          "subtype": "success", "stop_reason": "refusal", "stop_details": {"category": "cyber"},
                          "modelUsage": {"claude-opus-5-5": {}}}))
        sys.exit(0)
    ver = cfg.get("ver", "5-5")
    used = "opus" if model == "fable" and cfg.get("no_fable") else model
    print(json.dumps({"result": res, "session_id": "sess-1", "total_cost_usd": 0.01, "is_error": False,
                      "modelUsage": {f"claude-{used}-{ver}": {}}}))
''')


# The ladder mechanics (effort restore, escalation, floors, probes) are tested from the cheap
# rungs, so these tests pin the v0.6 per-tier starts explicitly. Production starts every tier at
# the worker role (opus/medium): see test_every_tier_starts_at_the_worker_role.
LEGACY_START = {"T0": "haiku/medium", "T1": "sonnet/medium", "T2": "opus/low", "T3": "opus/medium"}


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
                            "--baseline", "test -f target.txt", *extra,
                            *([] if "--start" in extra else ["--start", LEGACY_START[tier]]), "do the thing"],
                           env=env, capture_output=True, text=True)
        summary = json.loads(p.stdout) if p.stdout.strip() else {}
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return p.returncode, summary, calls, p.stderr

    def route(self, ttype, tier, **kw):
        return self.router.route(ttype, tier, start=LEGACY_START[tier], **kw)

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
        self.assertEqual(self.route("bugfix:api", "T1")["model"], "sonnet")
        self.assertEqual([p["kind"] for p in self.router.pending()], ["raise_floor"])

        self.router.approve(s["proposals"][0]["id"])
        d = self.route("bugfix:api", "T1")
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

    def test_every_tier_starts_at_the_worker_role(self):
        for tier in ("T0", "T1", "T2", "T3"):
            d = self.router.route("feature:web", tier)
            self.assertEqual((d["model"], d["effort"], d["source"]), ("opus", "medium", "tier-default"), tier)
        code, s, calls, err = self.run_task("rename:web", "T0", {"pass": ["opus/medium"]},
                                            extra=("--start", "opus/medium"))
        self.assertEqual(code, 0, err)
        self.assertEqual([(c["model"], c["effort"]) for c in calls], [("opus", "medium")])
        # a failure at the worker start re-runs at opus/high (effort) before a stronger model
        code, s, calls, err = self.run_task("bugfix:web", "T1", {"pass": ["opus/high"], "cause": "effort"},
                                            extra=("--start", "opus/medium"))
        self.assertEqual(code, 0, err)
        self.assertEqual([(c["model"], c["effort"]) for c in calls if not c["classifier"]][-1], ("opus", "high"))
        os.environ["ORCHESTRATE_MODEL_WORKER"] = "sonnet/high"
        try:
            self.assertEqual(self.router.route("feature:web", "T1")["model"], "sonnet")
        finally:
            del os.environ["ORCHESTRATE_MODEL_WORKER"]

    def test_advisor_workers_are_told_when_to_consult(self):
        code, s, calls, err = self.run_task("feature:web", "T1", {"pass": ["opus/high"], "cause": "effort"},
                                            extra=("--start", "opus/medium", "--advisor", "fable"))
        self.assertEqual(code, 0, err)
        workers = [c for c in calls if not c["classifier"]]
        self.assertTrue(all(c["advisor"] == "fable" and c["consult"] for c in workers), workers)
        self.assertEqual({st["advisor"] for st in s["steps"]}, {"fable"})
        # no advisor: no consult line
        _, _, calls, _ = self.run_task("feature:web", "T1", {"pass": ["opus/medium"]}, extra=("--start", "opus/medium"))
        self.assertFalse(any(c["consult"] for c in calls))

    def test_max_model_caps_the_advisor(self):
        import models
        self.assertEqual(models.advisor_args("opus", "fable", "opus"), ["--advisor", "opus"])
        self.assertEqual(models.advisor_args("opus", "fable", None), ["--advisor", "fable"])
        self.assertEqual(models.advisor_args("opus", "sonnet", None), [], "an advisor below the worker is dropped")

    def test_advisor_refused_at_launch_runs_on_without_it(self):
        code, s, calls, err = self.run_task("feature:web", "T1", {"pass": ["opus/medium"], "advisor_launch_fail": True},
                                            extra=("--start", "opus/medium", "--advisor", "fable"))
        self.assertEqual(code, 0, err)
        workers = [c for c in calls if not c["classifier"]]
        self.assertEqual([(c["advisor"], c["consult"]) for c in workers], [("fable", True), (None, False)])
        self.assertIn("usage credits", s["advisor_off"])
        self.assertEqual([(st["config"], st["ok"]) for st in s["steps"]], [("opus/medium", True)],
                         "the refused launch is not a failed attempt for the model")
        self.assertTrue(self.ledger()[-1]["ok"])

    def test_refusal_stops_the_task_and_is_not_evidence(self):
        code, s, calls, err = self.run_task("feature:web", "T1", {"refuse": True}, extra=("--start", "opus/medium"))
        self.assertEqual(code, 9, err)
        self.assertEqual((s["result"], s["refusal_category"]), ("refused", "cyber"))
        self.assertEqual(len([c for c in calls if not c["classifier"]]), 1, "no retry, no escalation")
        self.assertFalse(any(c["classifier"] for c in calls), "nothing to classify")
        self.assertFalse((self.state / "ledger.jsonl").exists() and self.ledger(), "no routing evidence")

    def test_cut_off_reply_is_never_a_verdict(self):
        import claude_cli
        d = {"stop_reason": "max_tokens", "is_error": False, "subtype": "success",
             "structured_output": {"verdict": "pass"}}
        self.assertEqual(claude_cli._kind(d), "cut_off")
        self.assertEqual(claude_cli._kind({"stop_reason": "refusal", "subtype": "success"}), "refusal")
        self.assertIsNone(claude_cli._kind({"stop_reason": "end_turn", "subtype": "success"}))

    def test_floors_from_the_old_ladder_still_route(self):
        # v1 stored floors as indexes into haiku/low..opus/xhigh; 4 was opus/high
        (self.state / "routing.json").write_text(json.dumps({"bugfix:api": {
            "floor": 4, "approved_at": __import__("time").time(), "model_alias": "sonnet"}}))
        d = self.route("bugfix:api", "T1")
        self.assertEqual((d["model"], d["effort"], d["source"]), ("opus", "high", "learned"))
        (self.state / "ledger.jsonl").write_text(json.dumps({
            "t": 0, "task_id": "x", "type": "bugfix:api", "tier": "T1", "rung": 4, "model": "opus",
            "effort": "high", "ok": True}) + "\n")
        self.assertEqual(self.router._ledger()[0]["rung"], self.router.LADDER.index(("opus", "high")))

    def test_role_defaults_follow_anthropic_guidance(self):
        import models
        self.assertEqual(models.role("classifier"), ("haiku", "low"))
        self.assertEqual(models.role("manager"), ("opus", "medium"))
        self.assertEqual(models.role("planner"), ("fable", "medium"))
        self.assertEqual(models.role("planner", {"max_model": "opus"}), ("opus", "high"))
        # the user's assignment (2026-11-16)
        self.assertEqual(models.role("advisor"), ("fable", "medium"))
        self.assertEqual(models.ADVISOR_ESCALATE, "high")
        self.assertEqual(models.role("advisor", {"max_model": "opus"}), ("opus", "high"))
        for r in ("manager", "worker", "reviewer", "area_reviewer", "cleanup"):
            self.assertEqual(models.role(r), ("opus", "medium"), r)
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
        self.assertEqual(self.route("bugfix:mobile", "T1")["source"], "learned")
        self.router.note_alias("sonnet", "claude-sonnet-5-6")  # new Sonnet ships
        d = self.route("bugfix:mobile", "T1")
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
        d = self.route("refactor:web", "T1")
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
            self.route("stuff:api", "T1")
        with self.assertRaises(SystemExit):
            self.route("bugfix:Not A Slug", "T1")
        self.assertEqual(self.route("bugfix:billing-service", "T1")["model"], "sonnet")

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

    def _argv_fake(self, reply: dict) -> Path:
        """A fake claude that records its argv and prints `reply` as raw UTF-8, like Node does."""
        fake, argv = self.tmp / "argv-claude", self.tmp / "argv.json"
        fake.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, pathlib, sys
            pathlib.Path({str(argv)!r}).write_text(json.dumps(sys.argv[1:]))
            sys.stdout.buffer.write(json.dumps({reply!r}, ensure_ascii=False).encode() + b"\\n")
        """))
        fake.chmod(0o755)
        return fake

    def _cli_call(self, fake: Path, env: dict, **kw):
        import claude_cli
        cc = importlib.reload(claude_cli)
        saved = {k: os.environ.get(k) for k in ("ROUTER_CLAUDE_BIN", *env)}
        os.environ.update({"ROUTER_CLAUDE_BIN": str(fake), **env})
        try:
            r = cc.call("x", "sonnet", "low", self.repo, perm="dontAsk", tools=["Read"], **kw)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        return r, json.loads((self.tmp / "argv.json").read_text())

    def test_line_separators_in_the_reply_still_parse(self):
        verdict = {"summary": "first\u2028second\u2029third", "ok": True}
        fake = self._argv_fake({"is_error": False, "subtype": "success", "result": "a\u2028b\u2029c",
                                "structured_output": verdict, "num_turns": 1})
        r, _ = self._cli_call(fake, {}, schema={"type": "object"})
        self.assertFalse(r["is_error"])
        self.assertIsNone(r["error_kind"])
        self.assertEqual(r["structured"], verdict)
        self.assertEqual(r["result"], "a\u2028b\u2029c")

    def test_reply_after_log_lines_still_parses(self):
        fake = self._argv_fake({"is_error": False, "subtype": "success", "result": "ok"})
        fake.write_text(fake.read_text().replace("sys.stdout.buffer.write(", "print('warming up'); sys.stdout.flush(); sys.stdout.buffer.write(", 1))
        r, _ = self._cli_call(fake, {})
        self.assertIsNone(r["error_kind"])
        self.assertEqual(r["result"], "ok")

    def test_hermetic_drops_mcp_servers_and_hooks_only_when_asked(self):
        fake = self._argv_fake({"is_error": False, "subtype": "success", "result": "ok"})
        own = {"permissions": {"deny": ["Bash"]}}
        _, argv = self._cli_call(fake, {"ORCHESTRATE_HERMETIC": "1"}, settings=own)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(json.loads(argv[argv.index("--mcp-config") + 1]), {"mcpServers": {}})
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {**own, "disableAllHooks": True})
        self.assertNotIn("--setting-sources", argv, "the user's CLAUDE.md and rules still load")
        _, argv = self._cli_call(fake, {"ORCHESTRATE_HERMETIC": "1"})
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"disableAllHooks": True})
        for env in ({}, {"ORCHESTRATE_HERMETIC": "0"}):
            _, argv = self._cli_call(fake, env, settings=own)
            self.assertNotIn("--strict-mcp-config", argv)
            self.assertNotIn("--mcp-config", argv)
            self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), own)

    def test_a_reply_that_is_json_but_not_an_object_is_unparseable(self):
        fake = self._argv_fake({"is_error": False, "subtype": "success", "result": "ok"})
        fake.write_text(fake.read_text().replace("sys.stdout.buffer.write(", "print('oops'); print(42); sys.exit(0); sys.stdout.buffer.write(", 1))
        r, _ = self._cli_call(fake, {}, retries=0)
        self.assertEqual(r["error_kind"], "unparseable")
        self.assertTrue(r["is_error"])
        self.assertIsNone(r["structured"])

    def test_a_learned_note_skips_headings_inside_code_fences_and_keeps_crlf(self):
        agents = self.repo / "AGENTS.md"
        agents.write_bytes(b"# Repo\r\n\r\n## Agent notes (learned)\r\n- old\r\n\r\n```bash\r\n"
                           b"# regenerate the stubs\r\nmake stubs\r\n```\r\n- fenced note\r\n\r\n## Human\r\nx\r\n")
        p = self.router.propose_context("feature:api", str(agents), "NEW NOTE", "job j1: evidence")
        self.router.approve(p["id"])
        self.assertEqual(agents.read_bytes(),
                         b"# Repo\r\n\r\n## Agent notes (learned)\r\n- old\r\n\r\n```bash\r\n"
                         b"# regenerate the stubs\r\nmake stubs\r\n```\r\n- fenced note\r\n- NEW NOTE\r\n\r\n"
                         b"## Human\r\nx\r\n")

    def _approve_note(self, agents: Path, text: str) -> str:
        p = self.router.propose_context("feature:api", str(agents), text, "job j1: evidence")
        self.router.approve(p["id"])
        return agents.read_text()

    def test_learned_note_goes_in_its_section_not_in_a_later_human_one(self):
        agents = self.repo / "AGENTS.md"
        head = "# Repo\n\nRun `make test`.\n\n## Agent notes (learned)\n- first note\n\n"
        deploy = "## Deploy\n\nAsk Sam before deploying.\n"
        agents.write_text(head + deploy)
        out = self._approve_note(agents, "second note")
        self.assertTrue(out.endswith("\n\n" + deploy), out)
        self.assertEqual(out, "# Repo\n\nRun `make test`.\n\n## Agent notes (learned)\n"
                              "- first note\n- second note\n\n" + deploy)
        out = self._approve_note(agents, "third note")
        self.assertIn("- second note\n- third note\n\n## Deploy", out)
        self.assertTrue(out.endswith(deploy))

    def test_learned_note_section_is_created_or_extended_at_eof(self):
        agents = self.repo / "AGENTS.md"
        agents.write_text("# Repo\n\nRun `make test`.\n")
        out = self._approve_note(agents, "one")
        self.assertEqual(out, "# Repo\n\nRun `make test`.\n\n## Agent notes (learned)\n- one\n")
        out = self._approve_note(agents, "two")
        self.assertEqual(out, "# Repo\n\nRun `make test`.\n\n## Agent notes (learned)\n- one\n- two\n")
        agents.write_text("## Agent notes (learned)\n### Go\n- three\n")
        self.assertEqual(self._approve_note(agents, "four"),
                         "## Agent notes (learned)\n### Go\n- three\n- four\n", "### stays inside the section")

    def test_budget_stops_the_task(self):
        fake = self.tmp / "claude"
        fake.write_text(FAKE_CLAUDE.replace('print(json.dumps({"result": res,',
                                            'print(json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "result": res,')
                        .replace('"is_error": False,', ""))
        code, s, calls, err = self.run_task("feature:web", "T1", {"pass": []})
        self.assertEqual(code, 7, err)
        self.assertEqual(s["result"], "budget")
        self.assertFalse(any(c["classifier"] for c in calls), "budget is not a failure to classify")

    def test_status_sums_the_advisor_re_check_yield(self):
        status = lambda: subprocess.run([sys.executable, str(SCRIPTS / "router.py"), "status"],  # noqa: E731
                                        env=self.env, capture_output=True, text=True, check=True).stdout
        self.assertNotIn("Advisor high re-check", status(), "no advice.jsonl: no line")
        rows = [{"job": "a", "calls": 2, "escalated": 1, "found": 1, "escalation_cost_usd": 0.5},
                {"job": "b", "calls": 3, "escalated": 2, "found": 0, "escalation_cost_usd": 1.25}]
        (self.state / "advice.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        self.assertIn("Advisor high re-check: found blocking issues in 1 of 3 re-checks over 2 job(s), "
                      "$1.75 total", status())

if __name__ == "__main__":
    unittest.main()
