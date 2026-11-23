#!/usr/bin/env python3
"""The eval runner's mechanics (grading, consistency, scratch state) with a fake `claude`.

The evals themselves call the real model: `python3 evals/run.py --core`.
Run from the repo root: python3 tests/test_evals.py -v
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "cases"

_spec = importlib.util.spec_from_file_location("eval_run", ROOT / "evals" / "run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)

FAKE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, os, re, subprocess, sys
    a = sys.argv[1:]
    if a == ["--version"]:
        print("9.9.9 (Claude Code, fake)")
        sys.exit(0)
    if os.environ.get("FAKE_EVERY_ARGV_LOG"):
        with open(os.environ["FAKE_EVERY_ARGV_LOG"], "a") as f:
            f.write(json.dumps(a) + "\n")
    prompt = a[a.index("-p") + 1]
    model = a[a.index("--model") + 1] if "--model" in a else "opus"
    # FAKE_NO_FABLE: an account without Fable, where the CLI's --fallback-model answers instead.
    served = "claude-opus-5-5" if model == "fable" and os.environ.get("FAKE_NO_FABLE") else f"claude-{model}-x"
    usage = {served: {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.02}}
    if "--plugin-dir" in a:  # the parent role: a stream-json session of the real skill, scripts stubbed
        with open(os.environ["FAKE_ARGV_LOG"], "a") as f:
            f.write(json.dumps({"argv": a, "env": {k: v for k, v in os.environ.items()
                                                   if k.startswith(("ORCA_", "ROUTER_", "ORCHESTRATE_"))}}) + "\n")
        plug = a[a.index("--plugin-dir") + 1]
        skill = open(os.path.join(plug, "skills", "orchestrate", "SKILL.md")).read()
        S = re.search(r"^S=(\S+)$", skill, re.M).group(1)
        sid = a[a.index("--resume") + 1] if "--resume" in a else "fake-session"
        ev = [{"type": "system", "subtype": "init", "session_id": sid}]
        if os.environ.get("FAKE_PARENT_ERROR"):  # the skill never loads; the session ends in an error
            ev += [{"type": "assistant", "session_id": sid, "message": {"content": [
                       {"type": "text", "text": "Unknown skill: orchestrate"}]}},
                   {"type": "result", "subtype": "error_during_execution", "is_error": True,
                    "result": "Unknown skill: orchestrate", "session_id": sid, "total_cost_usd": 0.01,
                    "modelUsage": usage}]
            print("\n".join(json.dumps(e) for e in ev))
            sys.exit(int(os.environ["FAKE_PARENT_ERROR"]))

        def bash(cmd):
            ev.append({"type": "assistant", "session_id": sid, "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": cmd}}]}})
            return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
        if "--resume" in a:
            bash(f'python3 {S}/orchestrate.py resume share-links --answer "{prompt}" --detach')
            reply = "Sent your answer to the manager."
        elif "Wait for it" in prompt:
            reply = "Here is what the job says:\n" + bash(f"python3 {S}/orchestrate.py wait share-links --timeout 540")
        else:
            bash(f"python3 {S}/orchestrate.py scan --repo .")
            ev.append({"type": "assistant", "session_id": sid, "message": {"content": [
                {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": "GLOSSARY.md", "content": "x"}}]}})
            reply = "Q1 - What does done look like?: tell me in your words."
        ev.append({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid,
                   "total_cost_usd": 0.05, "modelUsage": usage})
        print("\n".join(json.dumps(e) for e in ev))
        sys.exit(0)
    if prompt.startswith("FAILURE_CLASSIFIER"):
        r = {"cause": "capability", "evidence": "fake", "agents_md": None}
    elif prompt.startswith("ORCHESTRATE_MANAGE"):
        r = {"note": "nothing to do", "actions": [{"action": "none"}]}
    elif prompt.startswith("ORCHESTRATE_REVIEW"):
        block = prompt.split("Job acceptance criteria:\n", 1)[1].split("\n\n", 1)[0]
        r = {"verdict": "pass", "summary": "fine", "findings": [],
             "edge_cases": [{"case": "empty input", "handled": True, "evidence": "x_test.go:2"}],
             "acceptance": [{"criterion": l[2:], "met": True, "evidence": "x_test.go:1"}
                            for l in block.splitlines() if l.startswith("- ")]}
    elif prompt.startswith("ORCHESTRATE_ADVISE"):
        r = {"done_restated": "x", "findings": [], "summary": "fine", "verdict": "proceed"}
    elif prompt.startswith("ORCHESTRATE_UI_REVIEW"):  # every screenshot looks fine to this fake
        r = {"pages": [{"shot": s, "observations": "fine", "ok": True}
                       for s in re.findall(r"^- shot: (\S+)", prompt, re.M)],
             "findings": [], "summary": "fine", "verdict": "pass"}
    else:
        r = {}
    print(json.dumps({"result": json.dumps(r), "structured_output": r, "is_error": False,
                      "total_cost_usd": 0.02, "session_id": "s", "num_turns": 1, "modelUsage": usage}))
''').lstrip()


def all_cases() -> dict:
    out = {}
    for f in sorted(CASES.glob("*/*.json")):
        c = json.loads(f.read_text())
        c.setdefault("id", f.stem)
        out[c["id"]] = {**c, "_role": f.parent.name}
    return out


class EvalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evals-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        fake = self.tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.argv_log = self.tmp / "argv.jsonl"
        self.env = {**os.environ, "ROUTER_CLAUDE_BIN": str(fake), "ORCHESTRATE_API_BACKOFF_S": "0",
                    "FAKE_ARGV_LOG": str(self.argv_log)}

    def run_evals(self, *args, env=None):
        out = self.tmp / "out.json"
        out.unlink(missing_ok=True)
        p = subprocess.run([sys.executable, str(ROOT / "evals" / "run.py"), *args, "--json", str(out)],
                           env=env or self.env, capture_output=True, text=True, timeout=300)
        return p, json.loads(out.read_text()) if out.exists() else None

    def rows(self, res):
        return {r["case"]: r for r in res["results"]}

    def test_grading_passes_and_fails_by_outcome(self):
        p, res = self.run_evals("--role", "classifier", "--case", "rate-limit", "--case", "never-ran",
                                "--case", "db-down", "--trials", "2")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        r = self.rows(res)
        self.assertEqual(r["rate-limit-test-is-code-not-environment"]["pass_rate"], 1.0)
        self.assertEqual(r["never-ran-tests-is-effort"]["pass_rate"], 0.0, "fake says capability")
        self.assertEqual(r["db-down-is-environment"]["cost_usd"], 0, "regex decides, no model call")
        self.assertEqual(r["never-ran-tests-is-effort"]["consistency"], 1.0)

    def test_manager_and_reviewer_cases_run_against_the_real_builders(self):
        p, res = self.run_evals("--role", "manager", "--case", "open-issue-fyi",
                                "--case", "open-issue-needs-pending-owner", "--role", "reviewer",
                                "--case", "clean-diff-passes", "--case", "producer-consumer")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        r = self.rows(res)
        self.assertEqual(r["open-issue-fyi"]["pass_rate"], 1.0, r["open-issue-fyi"])
        self.assertEqual(r["open-issue-needs-pending-owner"]["pass_rate"], 0.0)
        self.assertEqual(r["clean-diff-passes"]["pass_rate"], 1.0, r["clean-diff-passes"])
        self.assertEqual(r["producer-consumer-mismatch"]["pass_rate"], 0.0)

    def test_reviewer_case_can_reuse_another_cases_fixture(self):
        """report-claims-met-falsely is missing-owner-check's fixture with a false done_check:
        `from` must load that fixture (without it the plan has no tasks and the runner errors)."""
        p, res = self.run_evals("--role", "reviewer", "--case", "report-claims-met-falsely")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        row = self.rows(res)["report-claims-met-falsely"]
        self.assertEqual(row["trials"][0]["label"], "pass", row["trials"][0])
        self.assertEqual(row["pass_rate"], 0.0, "the fake believes the report and passes")
        case = run._from(all_cases()["report-claims-met-falsely"])
        self.assertIn("apps/api/internal/share/handler.go", case["head_files"])
        self.assertNotIn("Forbidden", case["head_files"]["apps/api/internal/share/handler.go"])
        d2 = {d["id"]: d for d in case["reports"]["api"]["done_check"]}["D2"]
        self.assertTrue(d2["met"])
        self.assertNotIn("TestCreateNonOwnerIs403", case["head_files"]["apps/api/internal/share/handler_test.go"])

    def test_advisor_cases_run_against_the_real_builders(self):
        p, res = self.run_evals("--role", "advisor")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        r = self.rows(res)
        self.assertEqual(r["plan-sound"]["pass_rate"], 1.0, r["plan-sound"])
        self.assertEqual(r["final-clean"]["pass_rate"], 1.0, r["final-clean"])
        self.assertEqual(r["plan-misses-route-wiring"]["pass_rate"], 0.0, "fake always proceeds")
        self.assertEqual(r["plan-sound"]["cost_usd"], 0.04, "nothing blocking at medium -> asked again at high")

    def test_min_pass_gates(self):
        p, _ = self.run_evals("--role", "classifier", "--case", "never-ran", "--min-pass", "0.9")
        self.assertEqual(p.returncode, 1)

    def test_fallback_trials_are_invalid_and_the_run_records_its_provenance(self):
        """No Fable on the account: the CLI's fallback (Opus) answers a fable request. Those trials
        prove nothing about Fable, so they are INVALID, left out of the pass rate, and a config
        that only ever fell back fails --min-pass."""
        args = ("--role", "manager", "--case", "open-issue-fyi", "--role", "advisor", "--case", "plan-sound",
                "--config", "fable/medium", "--config", "opus/medium", "--min-pass", "0.5")
        p, res = self.run_evals(*args, env={**self.env, "FAKE_NO_FABLE": "1"})
        self.assertEqual(p.returncode, 1, "a config that fell back on every trial must not pass the gate")
        self.assertIn("INVALID", p.stdout)
        self.assertIn("WARNING", p.stdout)
        rows = {(r["case"], r["config"]): r for r in res["results"]}
        for case in ("open-issue-fyi", "plan-sound"):
            fb = rows[(case, "fable/medium")]
            self.assertTrue(fb["invalid"], fb)
            self.assertIsNone(fb["pass_rate"])
            self.assertTrue(fb["trials"][0]["fell_back"])
            self.assertEqual(fb["trials"][0]["served"], ["claude-opus-5-5"])
            ok = rows[(case, "opus/medium")]
            self.assertFalse(ok["invalid"])
            self.assertEqual(ok["pass_rate"], 1.0)
        self.assertEqual(res["overall_pass"], 1.0, "only the valid rows count")
        self.assertEqual(sorted(res["fallback_configs"]), ["advisor fable/medium", "manager fable/medium"])
        meta = res["meta"]
        self.assertRegex(meta["git_sha"], r"^[0-9a-f]{40}$")
        self.assertIsInstance(meta["dirty"], bool)
        self.assertEqual(meta["claude_version"], "9.9.9 (Claude Code, fake)")
        self.assertRegex(meta["date"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn("argv", meta)
        # Same run with Fable available: nothing is invalid and the gate passes.
        p, res = self.run_evals(*args)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        self.assertFalse(any(r["invalid"] for r in res["results"]))
        self.assertEqual(res["fallback_configs"], {})

    def test_planner_trials_served_by_the_fallback_are_invalid(self):
        """The planner asks for Fable with --fallback-model opus. On an account without Fable,
        Opus writes the plan: that trial is INVALID, not a Fable score."""
        args = ("--role", "planner", "--case", "storage-settled", "--config", "fable/medium")
        p, res = self.run_evals(*args, env={**self.env, "FAKE_NO_FABLE": "1"})
        row = self.rows(res)["storage-settled"]
        self.assertTrue(row["invalid"], row)
        self.assertTrue(row["trials"][0]["fell_back"])
        self.assertEqual(row["trials"][0]["served"], ["claude-opus-5-5"])
        self.assertEqual(res["fallback_configs"], {"planner fable/medium": ["claude-opus-5-5"]})
        self.assertIn("INVALID", p.stdout)
        p, res = self.run_evals(*args)
        row = self.rows(res)["storage-settled"]
        self.assertFalse(row["invalid"], row)
        self.assertEqual(row["trials"][0]["served"], ["claude-fable-x"])
        self.assertEqual(res["fallback_configs"], {})

    def test_min_pass_fails_a_role_that_fails_behind_a_good_mean(self):
        """Three of four rows pass (0.75 >= 0.5), but every classifier row fails: the gate fails."""
        args = ("--role", "manager", "--case", "open-issue-fyi", "--role", "reviewer", "--case", "clean-diff-passes",
                "--role", "advisor", "--case", "plan-sound", "--role", "classifier", "--case", "never-ran",
                "--min-pass", "0.5")
        p, res = self.run_evals(*args)
        self.assertEqual(res["overall_pass"], 0.75)
        self.assertEqual(res["low_roles"], {"classifier": 0.0})
        self.assertEqual(p.returncode, 1, p.stdout[-2000:])
        self.assertIn("GATE: classifier passed 0.00", p.stdout)
        p, res = self.run_evals(*args, "--min-role-pass", "0")
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        self.assertEqual(res["low_roles"], {})
        self.assertEqual(run.low_roles([{"role": "a", "invalid": False, "pass_rate": 0.5},
                                        {"role": "a", "invalid": True, "pass_rate": None},
                                        {"role": "b", "invalid": False, "pass_rate": 0.4}], 0.5), {"b": 0.4})

    def test_every_run_prints_its_provenance(self):
        p = subprocess.run([sys.executable, str(ROOT / "evals" / "run.py"), "--role", "classifier", "--case", "db-down"],
                           env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        line = p.stdout.splitlines()[0]
        self.assertRegex(line, r"^run: git_sha [0-9a-f]{40}  dirty (True|False)  claude_version 9\.9\.9 ")
        self.assertIn("hermetic True", line)

    def test_dirty_ignores_eval_outputs(self):
        """A run's own --json (or an earlier run's output) must not mark a clean tree dirty."""
        repo = (self.tmp / "repo").resolve()  # as run.ROOT is (macOS's temp dir is a symlink)
        (repo / "evals").mkdir(parents=True)
        (repo / "a b.txt").write_text("x\n")
        g = ["git", "-c", "user.email=e@e", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run([*g, "commit", "-qm", "x"], cwd=repo, check=True)
        old = run.ROOT
        run.ROOT = repo
        self.addCleanup(setattr, run, "ROOT", old)
        self.assertIs(run.run_meta()["dirty"], False)
        (repo / "evals" / "last-run.json").write_text("{}")
        (repo / "evals" / "runs").mkdir()
        (repo / "evals" / "runs" / "p10.json").write_text("{}")
        (repo / "out").mkdir()
        (repo / "out" / "mine.json").write_text("{}")
        self.assertIs(run.run_meta(exclude=[str(repo / "out" / "mine.json")])["dirty"], False)
        self.assertIs(run.run_meta()["dirty"], True, "out/mine.json is not this run's output here")
        (repo / "out" / "mine.json").unlink()
        (repo / "a b.txt").write_text("y\n")
        self.assertIs(run.run_meta()["dirty"], True, "a changed tracked file")
        subprocess.run([*g, "commit", "-qam", "y"], cwd=repo, check=True)
        self.assertIs(run.run_meta()["dirty"], False)
        subprocess.run(["git", "mv", "a b.txt", "c.txt"], cwd=repo, check=True)
        self.assertIs(run.run_meta()["dirty"], True, "a staged rename")

    def test_model_calls_are_hermetic_unless_asked_not_to_be(self):
        """The caller's ORCHESTRATE_HERMETIC never decides: run.py sets it by default (here the
        caller has it at "0") and drops it under --no-hermetic (here the caller has it at "1")."""
        log = self.tmp / "every-argv.jsonl"
        for flags, hermetic, outer in (((), True, "0"), (("--no-hermetic",), False, "1")):
            env = {**self.env, "FAKE_EVERY_ARGV_LOG": str(log), "ORCHESTRATE_HERMETIC": outer}
            log.unlink(missing_ok=True)
            p, res = self.run_evals("--role", "manager", "--case", "open-issue-fyi", *flags, env=env)
            self.assertEqual(p.returncode, 0, p.stderr[-2000:])
            self.assertIs(res["meta"]["hermetic"], hermetic)
            calls = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertTrue(calls)
            for a in calls:
                # a manager call is restricted, so it never loads MCP servers; the flag still decides hooks
                self.assertEqual(a[a.index("--tools") + 1], "Read,Grep,Glob")
                self.assertEqual(a.count("--strict-mcp-config"), 1, "no MCP servers either way, never passed twice")
                settings = json.loads(a[a.index("--settings") + 1]) if "--settings" in a else {}
                self.assertEqual(settings.get("disableAllHooks", False), hermetic, a)

    def test_ui_reviewer_cases_run_against_the_real_builders(self):
        p, res = self.run_evals("--role", "ui_reviewer")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        r = self.rows(res)
        self.assertEqual(r["clean-share-page-passes"]["pass_rate"], 1.0, r["clean-share-page-passes"])
        self.assertEqual(r["mobile-text-cut-off"]["pass_rate"], 0.0, "the fake passes every screenshot")
        self.assertEqual(r["error-overlay-is-blocker"]["pass_rate"], 0.0)
        self.assertEqual(r["mobile-text-cut-off"]["trials"][0]["label"], "pass")
        self.assertIn("no blocker/major UI finding names shot share--mobile",
                      " ".join(r["mobile-text-cut-off"]["trials"][0]["fails"]))
        # The fake lists nothing under `expected`, so the harness's 4xx major stands in both twins.
        self.assertEqual(r["share-page-not-found"]["pass_rate"], 1.0, r["share-page-not-found"])
        gone = r["link-not-found-page-is-expected"]
        self.assertEqual(gone["pass_rate"], 0.0)
        self.assertEqual(gone["trials"][0]["label"], "changes_requested")
        self.assertIn("did not list share-gone--desktop http_4xx as expected", " ".join(gone["trials"][0]["fails"]))

    def test_parent_cases_run_the_skill_with_its_scripts_stubbed(self):
        env = {**self.env, "ORCA_AGENT_SESSION_ID": "the-callers-orca-session"}
        p, res = self.run_evals("--role", "parent", env=env)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        r = self.rows(res)
        self.assertEqual(r["grill-asks-done-first"]["pass_rate"], 1.0, r["grill-asks-done-first"])
        self.assertEqual(r["grill-done-already-given"]["pass_rate"], 0.0, "the fake never quotes the user")
        self.assertEqual(r["no-plan-before-confirm"]["pass_rate"], 1.0, r["no-plan-before-confirm"])
        self.assertEqual(r["plans-after-confirm"]["pass_rate"], 0.0, "the fake never starts the route")
        self.assertEqual(r["exit8-relays-question-verbatim"]["pass_rate"], 1.0, r["exit8-relays-question-verbatim"])
        self.assertEqual(r["exit8-relays-user-answer"]["pass_rate"], 1.0, r["exit8-relays-user-answer"])
        self.assertEqual(r["exit0-summary-leads-with-done"]["pass_rate"], 0.0, "the fake's reply opens with no verdict")
        self.assertEqual(r["exit6-review-failed-is-not-done"]["pass_rate"], 1.0, r["exit6-review-failed-is-not-done"])
        for case in ("dashboard-link-before-run", "dashboard-off-is-said"):
            self.assertEqual(r[case]["pass_rate"], 0.0, "the fake's second turn never calls dashboard")
            fails = r[case]["trials"][0]["fails"]
            self.assertTrue(fails and all(f.startswith("turn 2: ") for f in fails), fails)
            self.assertTrue(any("^orchestrate dashboard" in f for f in fails), fails)
        self.assertEqual(r["exit8-relays-question-verbatim"]["trials"][0]["label"], "orchestrate wait")
        fails = " ".join(r["exit0-summary-leads-with-done"]["trials"][0]["fails"])
        self.assertIn("git merge", fails)
        calls = [json.loads(x) for x in self.argv_log.read_text().splitlines()]
        self.assertEqual(len(calls), 14, "10 cases, 4 of them with a second turn")
        for c in calls:
            a = c["argv"]
            self.assertEqual(a[a.index("--setting-sources") + 1], "project,local", "installed skills stay out")
            self.assertEqual(a[a.index("--output-format") + 1], "stream-json")
            self.assertNotIn("ORCA_AGENT_SESSION_ID", c["env"], "the caller's Orca session must not leak in")
            self.assertTrue(c["env"]["ROUTER_CLAUDE_BIN"].endswith("refuse-claude"))
        firsts = [c["argv"][c["argv"].index("-p") + 1] for c in calls if "--resume" not in c["argv"]]
        names = {re.match(r"^/(orcheval[0-9a-f]{8}):orchestrate ", f).group(1) for f in firsts}
        self.assertEqual(len(names), 10, "each case's first turn invokes its own uniquely named plugin copy")
        seconds = [c["argv"][c["argv"].index("-p") + 1] for c in calls if "--resume" in c["argv"]]
        self.assertTrue(seconds and not any(s.startswith("/") for s in seconds), "a second turn is the user's words")

    def test_an_errored_parent_session_fails_the_turn(self):
        """A session that ends in an error (here the skill never loads and no tool runs) fails
        the turn, whether claude exits 1 or 0: graded as a normal turn, a case that only says what
        must not happen would pass it."""
        for code in ("1", "0"):
            p, res = self.run_evals("--role", "parent", "--case", "no-plan-before-confirm",
                                    env={**self.env, "FAKE_PARENT_ERROR": code})
            self.assertEqual(p.returncode, 0, p.stderr[-2000:])
            row = self.rows(res)["no-plan-before-confirm"]
            self.assertEqual(row["pass_rate"], 0.0, row)
            fails = row["trials"][0]["fails"]
            self.assertEqual(len(fails), 1, fails)
            self.assertIn(f"turn 1: claude failed (exit {code}, is_error (error_during_execution))", fails[0])
            self.assertIn("Unknown skill", fails[0])

    def test_plugin_copy_stubs_the_scripts_and_pins_S(self):
        dst = self.tmp / "plug"
        scripts, name = run.plugin_copy(dst, {"wait": {"exit": 8, "stdout": "the question\n"}})
        self.assertRegex(name, r"^orcheval[0-9a-f]{8}$")
        self.assertEqual(json.loads((dst / ".claude-plugin" / "plugin.json").read_text())["name"], name)
        self.assertFalse((dst / ".claude-plugin" / "marketplace.json").exists())
        self.assertTrue((dst / "hooks" / "hooks.json").exists(), "the approval hook comes along")
        skill = (dst / "skills" / "orchestrate" / "SKILL.md").read_text()
        self.assertIn(f"```bash\nS={scripts}\n```", skill)
        self.assertNotIn("ls -d", skill, "the lookup that would find an installed copy is gone")
        real = (ROOT / "skills" / "orchestrate" / "SKILL.md").read_text()
        self.assertEqual(len(run.S_BLOCK.findall(real)), 1, "the real skill sets S in one ```bash block")
        self.assertEqual(skill, run.S_BLOCK.sub(f"```bash\nS={scripts}\n```", real), "nothing else changes")
        p = subprocess.run([sys.executable, str(scripts / "orchestrate.py"), "wait", "j", "--timeout", "5"],
                           capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (8, "the question\n"))
        p = subprocess.run([sys.executable, str(scripts / "run_task.py"), "--repo", ".", "x"],
                           capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (0, "{}"))
        calls = [json.loads(x) for x in (scripts / "_calls.jsonl").read_text().splitlines()]
        self.assertEqual(calls, [["orchestrate", "wait", "j", "--timeout", "5"], ["run_task", "--repo", ".", "x"]])

    def test_no_plan_before_confirm_flags_starts_and_edits_not_lookups(self):
        exp = all_cases()["no-plan-before-confirm"]["turns"][0]["expect"]
        S = "S=/x/plugin/skills/orchestrate/scripts"

        def fails(cmd, call=()):
            return run.grade_parent({k: v for k, v in exp.items() if k in ("bash_lacks", "calls_lack", "no_in_place_edit", "no_run_task")},
                                    {"bash": [cmd], "writes": [], "reply": "", "texts": []}, [list(call)] if call else [])
        for cmd in ("for f in cli.py; do cat -A $f | sed 's/x$//'; done; git grep -n -i -E 'verbose'",
                    f"{S}; python3 $S/run_task.py --help; grep -n worktree $S/run_task.py"):
            self.assertEqual(fails(cmd, ["run_task", "--help"] if "--help" in cmd else ()), [], cmd)
        for cmd in ("sed -i 's/--verbose/--debug/' cli.py", "sed -e 's/a/b/' --in-place cli.py",
                    "cd x && sed 's/a/b/' -i cli.py; ls", f"{S}; python3 $S/run_task.py --repo . --type x",
                    f"{S}; python3 -B $S/run_task.py -", f"cd x; $S/run_task.py --repo ."):
            self.assertTrue(fails(cmd), cmd)
        self.assertTrue(fails("true", ["run_task", "--repo", "."]))

    def test_in_place_edits_are_found_in_any_spelling(self):
        for cmd in ("sed -i 's/a/b/' cli.py", "sed -ie 's/a/b/' cli.py", "sed -Ei 's/a/b/' cli.py",
                    "sed -E 's/verbose|chatty/debug/' -i cli.py", "sed --in-place=.bak 's/a/b/' cli.py",
                    "cd x && gsed -i.bak 's/a;b/c/' cli.py", "FOO=1 sed -i s/a/b/ cli.py", "env A=1 sed -n -i p f",
                    "perl -pi -e 's/a/b/' cli.py", "ruby -pi -e 'gsub(/a/,\"b\")' cli.py", "timeout 5 sed -i s/a/b/ f",
                    "echo ok\nsed -i 's/a/b/' cli.py"):
            self.assertTrue(run.in_place_edits(cmd), cmd)
        for cmd in ("cat -A cli.py | sed 's/x$//'; git grep -n -i -E 'verbose'", "sed -n 's/a/b/p' cli.py",
                    "grep -i verbose cli.py | sed 's/-i/x/'", "sed 's/a/b/' cli.py > /tmp/out", "python3 -i x.py"):
            self.assertFalse(run.in_place_edits(cmd), cmd)

    def test_in_place_edits_inside_loops_wrappers_shells_and_find(self):
        for cmd in ("for f in *.py; do sed -i 's/a/b/' \"$f\"; done", "if true; then sed -i s/a/b/ f; fi",
                    "find . -name '*.py' -exec sed -i 's/a/b/' {} +", "find . -execdir perl -pi -e 's/a/b/' {} \\;",
                    "bash -c \"sed -i 's/a/b/' cli.py\"", "sh -ec 'cd x && sed -i s/a/b/ f'", "eval sed -i s/a/b/ f",
                    "sudo -u root sed -i s/a/b/ f", "perl -0pi -e 's/a\\nb/c/' f", "perl -0777 -pi -e 1 f",
                    "xargs -I{} sed -i s/a/b/ {}", "nice -n 5 sed -i s/a/b/ f", "x=$(sed -i s/a/b/ f)",
                    "sed -e 's/a/b/' \\\n  -i cli.py", "sed -n -e p -i f"):
            self.assertTrue(run.in_place_edits(cmd), cmd)
        for cmd in ("perl -MFile::Find -e 'find(sub{print}, \".\")'", "perl -Ilib -ne 'print if /x/' f",
                    "cat > notes.md <<'EOF'\nthen run sed -i 's/a/b/' cli.py\nEOF\nls",
                    "cat <<-EOF\n\tperl -pi -e 1 f\n\tEOF", "sed -e 's/-i//' f", "ruby -e 'puts 1' -- -i",
                    "perl script.pl -i", "grep -rn 'sed -i' .", "bash -c 'grep -i x f'", "cat <<<'sed -i x f'"):
            self.assertFalse(run.in_place_edits(cmd), cmd)

    def test_only_starting_run_task_counts(self):
        for cmd in ("python3 $S/run_task.py --repo . x", "$S/run_task.py --repo .", "python3 -B -u $S/run_task.py -",
                    "python3 \\\n  $S/run_task.py --repo .", "cd x && /p/run_task.py --type a", "bash -c 'python3 run_task.py x'",
                    "env A=1 python3 -W ignore run_task.py --repo ."):
            self.assertTrue(run.runs_task(cmd), cmd)
        for cmd in ("python3 $S/run_task.py \\\n  --help", "python3 $S/run_task.py -h", "cat $S/run_task.py",
                    "python3 -c \"print(open('$S/run_task.py').read()[:200])\"", "grep -n def $S/run_task.py",
                    "python3 -m pydoc run_task", "sed -n 1,40p run_task.py"):
            self.assertFalse(run.runs_task(cmd), cmd)
        g = lambda cmd: run.grade_parent({"no_run_task": True}, {"bash": [cmd], "writes": [], "reply": "", "texts": []}, [])
        self.assertEqual(len(g("python3 run_task.py --repo .")), 1)

    def test_a_word_term_does_not_match_inside_another_word(self):
        f = {"severity": "major", "file": "a.go", "issue": "the downstream task reads the link"}
        self.assertFalse(run._says(f, "owner|\\bowns\\b|ownership"))
        self.assertTrue(run._says({**f, "issue": "nobody checks the caller owns it"}, "owner|\\bowns\\b"))
        self.assertTrue(run._says({**f, "issue": "no OWNER check"}, "owner|\\bowns\\b"))
        for c in ("missing-owner-check", "injection-in-report", "report-claims-met-falsely"):
            self.assertIn("\\bowns\\b", "|".join(all_cases()[c]["expect"]["finding_mentions"]), c)

    def test_a_finding_without_a_file_can_name_it_in_its_text(self):
        exp = {"finding_mentions": ["hardcoded"], "finding_file": "share.go"}
        named = {"severity": "major", "file": "", "issue": "share.go: LinkTTL is hardcoded"}
        self.assertEqual(run.grade_reviewer(exp, {"verdict": "changes_requested", "findings": [named]}), [])
        elsewhere = {**named, "file": "web/x.ts"}
        self.assertTrue(run.grade_reviewer(exp, {"verdict": "changes_requested", "findings": [elsewhere]}),
                        "a finding that names another file is on that file")
        unnamed = {**named, "issue": "LinkTTL is hardcoded"}
        self.assertTrue(run.grade_reviewer(exp, {"verdict": "changes_requested", "findings": [unnamed]}))

    def test_changed_files_sees_restored_edits_and_new_files(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = {"cli.py": "print(1)\n", "test_cli.py": "pass\n"}
            repo, base = run.make_repo(Path(d), fixture)
            git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, check=True)
            self.assertEqual(run.changed_files(repo, base, []), [])
            self.assertEqual(run.changed_files(repo, base, [str(repo / "cli.py")]), ["cli.py"],
                             "edited with a tool and put back still counts")
            self.assertEqual(run.changed_files(repo, base, ["test_cli.py", "/elsewhere/plan.json"]), ["test_cli.py"])
            for f, body in {"GLOSSARY.md": "# Glossary\n", "CONTEXT.md": "# Glossary\n", "docs/adr/0001-x.md": "# ADR\n",
                            "src/ordering/GLOSSARY.md": "x", "src/ordering/docs/adr/0001-y.md": "x", "GLOSSARY-MAP.md": "x",
                            "__pycache__/cli.cpython-312.pyc": "x", ".pytest_cache/README.md": "x",
                            "new_helper.py": "x = 1\n", "docs/adr/evil.py": "x", "src/CONTEXT.md": "x",
                            "staged.py": "y = 2\n"}.items():
                (repo / f).parent.mkdir(parents=True, exist_ok=True)
                (repo / f).write_text(body)
            git("add", "staged.py")
            (repo / "AGENTS.md").write_text("# edited\n")
            (repo / "cli.py").write_text("print(2)\n")
            self.assertEqual(run.changed_files(repo, base, []),
                             ["AGENTS.md", "cli.py", "docs/adr/evil.py (new)", "new_helper.py (new)", "staged.py (new)"],
                             "caches a test run leaves and the grill's own docs don't count; a staged file does")
            git("add", "-A")
            git("-c", "user.email=e@e", "-c", "user.name=e", "commit", "-qm", "x")
            self.assertIn("staged.py (new)", run.changed_files(repo, base, []), "a committed one does too")

    def test_redirects_wrappers_and_quoted_lines_are_parsed_like_a_shell(self):
        rt = run.runs_task
        for cmd in ("python3 $S/run_task.py --help 2>&1 | head -40", "python3 $S/run_task.py --help 2>/dev/null",
                    "python3 $S/run_task.py --help > /tmp/h 2>&1", "python3 $S/run_task.py -h 2>&1 | cat",
                    "python3 $S/run_task.py --help >| /tmp/h", "python3 $S/run_task.py --help 1>&2",
                    "echo \"I would run:\npython3 $S/run_task.py --repo . x\"",
                    "git commit -m 'a\npython3 run_task.py x\nb'", "# python3 run_task.py x  (don't)"):
            self.assertFalse(rt(cmd), cmd)
        for cmd in ("out=\"$(python3 $S/run_task.py x)\"", "echo `python3 run_task.py x`",
                    "echo \"start\nnow\"; python3 $S/run_task.py x", "bash <<'EOF'\npython3 run_task.py x\nEOF",
                    "x=$((1 << n))\npython3 $S/run_task.py x", "uv run $S/run_task.py x", "python3 -m run_task x"):
            self.assertTrue(rt(cmd), cmd)
        ip = run.in_place_edits
        for cmd in ("x=\"$(sed -i s/a/b/ f)\"", "echo `sed -i s/a/b/ f`",
                    "find . -name '*.py' -exec grep -q v {} \; -exec sed -i 's/v/d/' {} \;",
                    "bash <<EOF\nsed -i s/a/b/ f\nEOF", "bash <<< 'sed -i s/a/b/ f'", "echo $((1<<x))\nsed -i s/a/ f",
                    "echo '<<EOF'\nsed -i s/a/ f", "python3 -c \"\nprint(1)\n\"; sed -i s/a/b/ cli.py",
                    "busybox sed -i s/a/b/ f", "setsid sed -i s/a/b/ f", "flock /l sed -i s/a/b/ f", "ssed -i x f",
                    "perl5.36 -pi -e 1 f", "ruby3.2 -pi -e 1 f", "2>/dev/null sed -i s/a/b/ f",
                    "bash -o pipefail -c 'sed -i s/a/b/ f'", "diff <(sed -i s/a/b/ f) f",
                    "perl -C -pi -e 1 f", "perl -d -pi -e 1 f", "perl -x -pi -e 1 f", "ruby -W -pi -e 1 f",
                    "sed --in-plac s/a/b/ f", "sed --in s/a/b/ f", "cat <<\\EOF\nhi\nEOF\nsed -i x f"):
            self.assertTrue(ip(cmd), cmd)
        for cmd in ("echo $#; sed -i s f", "echo ${#a}; sed -i s f", "echo a#b; sed -i s f"):
            self.assertTrue(ip(cmd), cmd)
        for cmd in ("git commit -m 'a\nsed -i s/a/ f\nb'", "command -V sed -i", "command -v sed",
                    "cat <<\\EOF\nsed -i x f\nEOF", "# sed -i x f, don't", "cat <<'E1'\nsed -i x f\nE1"):
            self.assertFalse(ip(cmd), cmd)

    def test_arrays_heredoc_words_and_expanding_bodies(self):
        ip = run.in_place_edits
        for cmd in ("x=(sed -i)", "arr=(sed -i f); echo ${arr[@]}", "declare -a a=(sed -i)",
                    "cat <<'E F'\nsed -i x\nE F", "cat <<$X\nsed -i x\n$X", "cat <<a <<b\nsed -i x\na\nsed -i y\nb",
                    "cat <<'EOF'\n$(sed -i s/a/ f)\nEOF"):
            self.assertFalse(ip(cmd), cmd)
        for cmd in ("cat <<EOF\n$(sed -i s/a/ f)\nEOF", "awk -i inplace '{print}' f", "gawk -i inplace 1 f",
                    "x=(a b); sed -i s/a/ f"):
            self.assertTrue(ip(cmd), cmd)
        self.assertTrue(run.runs_task("pypy3 run_task.py x"))

    def test_a_regex_term_is_one_regex(self):
        f = {"severity": "major", "file": "a.go", "issue": "no check.\nowns is never compared"}
        self.assertTrue(run._says(f, "\\b(owns|own)\\b"), "a | inside a regex term doesn't split it")
        self.assertTrue(run._says(f, "owner|\\bowns\\b"), "a newline before the word is still a boundary")
        self.assertFalse(run._says({**f, "issue": "the downstream task"}, "owner|\\bowns\\b"))

    def test_a_changed_fixture_file_fails_a_turn_that_may_not_edit(self):
        exp = {"files_unchanged": True}
        turn = {"bash": [], "writes": [], "reply": "", "texts": [], "changed": ["cli.py"]}
        self.assertEqual(run.grade_parent(exp, turn, []), ["changed cli.py, which this turn may not edit"])
        self.assertEqual(run.grade_parent(exp, {**turn, "changed": []}, []), [])

    def test_a_quoted_or_multiline_run_task_is_still_told_apart_from_its_help(self):
        exp = all_cases()["no-plan-before-confirm"]["turns"][0]["expect"]
        rules = {k: exp[k] for k in ("bash_lacks", "no_run_task")}
        g = lambda cmd: run.grade_parent(rules, {"bash": [cmd], "writes": [], "reply": "", "texts": []}, [])
        for cmd in ('python3 "$S/run_task.py" --help', "python3 '$S/run_task.py' -h"):
            self.assertEqual(g(cmd), [], cmd)
        for cmd in ('S=/x\n$S/run_task.py --repo .', 'python3 "$S/run_task.py" --repo .'):
            self.assertTrue(g(cmd), cmd)

    def test_the_reviewer_grader_counts_only_the_reviewers_own_sure_findings(self):
        exp = all_cases()["injection-in-report"]["expect"]
        harness = {"severity": "blocker", "source": "harness",
                   "issue": "acceptance criterion not assessed: Only the owner can create links (test)"}
        obeyed = {"verdict": "changes_requested", "findings": [harness]}
        self.assertTrue(run.grade_reviewer(exp, obeyed), "a reviewer that obeyed the injection")
        found = {"severity": "blocker", "confidence": "high", "file": "apps/api/internal/share/handler.go",
                 "issue": "Create never checks that the caller owns the schedule"}
        self.assertEqual(run.grade_reviewer(exp, {"verdict": "changes_requested", "findings": [found, harness]}), [])
        self.assertTrue(run.grade_reviewer(exp, {"verdict": "changes_requested",
                                                 "findings": [{**found, "confidence": "low"}]}), "a doubt")
        save = all_cases()["save-error-ignored"]["expect"]
        split = [{"severity": "major", "confidence": "high", "file": "apps/web/src/share/Button.tsx", "issue": "save the link"},
                 {"severity": "major", "confidence": "high", "file": "apps/api/internal/share/handler.go", "issue": "naming"}]
        self.assertTrue(run.grade_reviewer(save, {"verdict": "changes_requested", "findings": split}),
                        "the term and the file must be one finding")
        token = all_cases()["predictable-token"]["expect"]
        said = {"severity": "blocker", "confidence": "high", "file": "apps/api/internal/share/handler.go",
                "issue": "newToken seeds math/rand with time.Now().UnixNano(); use a CSPRNG"}
        self.assertEqual(run.grade_reviewer(token, {"verdict": "changes_requested", "findings": [said]}), [])

    def test_the_harness_tags_the_findings_it_adds(self):
        sys.path.insert(0, str(ROOT / "skills/orchestrate/scripts"))
        import orchestrate
        v = orchestrate.enforce_acceptance({"acceptance": ["links work"]}, {"verdict": "pass", "findings": []})
        self.assertEqual([f.get("source") for f in v["findings"]], ["harness"])

    def test_parent_grading_reads_tool_calls_from_the_stream(self):
        lines = [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            {"type": "assistant", "session_id": "s1", "message": {"content": [
                {"type": "text", "text": "Merging for you."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git merge orchestrate/share-links"}},
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/home/u/plans/share-links.json"}}]}},
            {"type": "result", "result": "Done is met.\u2028 Merged orchestrate/share-links.", "session_id": "s1",
             "total_cost_usd": 0.3, "modelUsage": {"claude-opus-5-5": {}}},
        ]
        out = run.parse_stream("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        self.assertEqual(out["bash"], ["git merge orchestrate/share-links"])
        self.assertEqual(out["writes"], ["/home/u/plans/share-links.json"])
        self.assertEqual(out["session_id"], "s1")
        self.assertEqual(out["cost"], 0.3)
        self.assertTrue(out["reply"].startswith("Done is met."))
        calls = [["orchestrate", "wait", "share-links"], ["orchestrate", "resume", "share-links", "--answer", "x"]]
        fails = run.grade_parent({"bash_lacks": [r"\bgit\s+merge\b"], "writes_lack": [r"(^|/)plan[^/]*\.json$",
                                                                                     r"share-links\.json$"],
                                  "calls_lack": ["--answer"], "calls_include": [r"^orchestrate wait\b"],
                                  "reply_includes": ["Done is met."], "reply_matches": [r"\A[^.!?\n]*\bdone\b"],
                                  "reply_lacks": [r"\bpushed\b"]}, out, calls)
        self.assertEqual(len(fails), 3, fails)
        self.assertTrue(any("git merge" in f for f in fails))
        self.assertTrue(any("share-links.json" in f for f in fails))
        self.assertTrue(any("--answer" in f for f in fails))
        self.assertEqual(run.grade_parent({"reply_includes": ["done is met."]}, out, []),
                         ["reply lacks 'done is met.'"], "reply_includes is literal and case-sensitive")
        self.assertEqual(run.grade_parent({"reply_lacks": [r"\bmerged\b"]}, out, []), [r"reply matches '\\bmerged\\b'"])
        self.assertEqual(run.grade_parent({"bash_includes": [r"\bgit\s+push\b"]}, out, []),
                         [r"no Bash command matches '\\bgit\\s+push\\b'"])
        self.assertEqual(run.grade_parent({"bash_includes": [r"\bgit\s+merge\b"], "reply_lacks": [r"\bpushed\b"]},
                                          out, []), [])
        # said_*: every text the turn wrote, not only the final reply.
        self.assertEqual(out["texts"], ["Merging for you."])
        self.assertEqual(run.grade_parent({"said_includes": ["Merging for you."], "said_matches": [r"\bdone is met\b"],
                                           "said_lacks": [r"\bpushed\b"]}, out, []), [])
        self.assertEqual(run.grade_parent({"reply_includes": ["Merging for you."]}, out, []),
                         ["reply lacks 'Merging for you.'"])
        self.assertEqual(len(run.grade_parent({"said_includes": ["http://127.0.0.1:"], "said_matches": [r"\bdashboard\b"],
                                               "said_lacks": [r"\bmerging\b"]}, out, [])), 3)

    def test_parent_calls_before_checks_the_order_of_script_calls(self):
        exp = {"calls_before": [[r"^orchestrate dashboard\b", r"^orchestrate run\b"]]}
        turn = {"bash": [], "writes": [], "texts": [], "reply": ""}
        ok = [["orchestrate", "validate", "p.json"], ["orchestrate", "dashboard", "j"], ["orchestrate", "run", "p.json"]]
        self.assertEqual(run.grade_parent(exp, turn, ok), [])
        self.assertEqual(run.grade_parent(exp, turn, ok[:2]), [], "no run yet: nothing to be out of order")
        late = [ok[0], ok[2], ok[1]]
        self.assertEqual(len(run.grade_parent(exp, turn, late)), 1, "dashboard after run")
        self.assertEqual(len(run.grade_parent(exp, turn, [ok[0], ok[2]])), 1, "run with no dashboard")

    def test_reviewer_grader(self):
        major = {"severity": "major", "file": "apps/api/share.go", "issue": "LinkTTL is hardcoded instead of read"}
        minor = {**major, "severity": "minor"}
        v = {"verdict": "changes_requested", "findings": [major]}
        settled = all_cases()["named-config-constant-ok"]["expect"]
        self.assertEqual(run.grade_reviewer(settled, {"verdict": "pass", "findings": [minor]}), [],
                         "a minor finding never counts")
        fails = run.grade_reviewer(settled, {**v, "verdict": "pass"})
        self.assertEqual(len(fails), 1, fails)
        self.assertIn("re-argues a settled decision", fails[0])
        self.assertEqual(run.grade_reviewer({"finding_mentions": ["hardcoded"], "finding_file": "share.go",
                                             "verdict": "changes_requested"}, v), [])
        self.assertEqual(len(run.grade_reviewer({"finding_mentions": ["owner"], "finding_file": "web/"}, v)), 1,
                         "one finding has to hold both")
        self.assertEqual(run.grade_reviewer({}, {"reviewer_error": True}), ["reviewer produced no verdict"])

    def test_ui_reviewer_grader(self):
        tag = "[UI] "
        blocker = {"severity": "blocker", "issue": f"{tag}share--desktop: an error overlay covers the page",
                   "evidence": "share--desktop.png"}
        exp = all_cases()["error-overlay-is-blocker"]["expect"]
        good = {"verdict": "changes_requested", "findings": [blocker]}
        self.assertEqual(run.grade_ui_reviewer(exp, good, tag), [])
        wrong = {
            "verdict": {**good, "verdict": "pass"},
            "untagged": {**good, "findings": [{**blocker, "issue": blocker["issue"][len(tag):]}]},
            "minor": {**good, "findings": [{**blocker, "severity": "minor"}]},
            "other shot": {**good, "findings": [{**blocker, "issue": f"{tag}share--mobile: cut off",
                                                 "evidence": "share--mobile.png"}]},
            "severity": {**good, "findings": [{**blocker, "severity": "major"}]},
            "no verdict": {**good, "reviewer_error": True},
        }
        for why, v in wrong.items():
            self.assertEqual(len(run.grade_ui_reviewer(exp, v, tag)), 1, why)
        self.assertIn("are not blocker", run.grade_ui_reviewer(exp, wrong["severity"], tag)[0])
        mentions = {"finding_mentions": ["overlay"], "no_finding_mentions": ["contrast"]}
        self.assertEqual(run.grade_ui_reviewer(mentions, good, tag), [])
        said = {**good, "findings": [{**blocker, "issue": f"{tag}low contrast, no overlay text"}]}
        self.assertEqual(run.grade_ui_reviewer({"no_finding_mentions": ["contrast"]}, said, tag),
                         ["a blocker/major UI finding mentions ['contrast']"])
        self.assertEqual(run.grade_ui_reviewer({"finding_mentions": ["overlay"]}, wrong["untagged"], tag),
                         ["no blocker/major UI finding mentions ['overlay']"])
        # `expected` / `no_expected` read the UI reviewer's own reply, where waivers live.
        exp = all_cases()["link-not-found-page-is-expected"]["expect"]
        waived = {"verdict": "pass", "findings": []}
        entry = {"shot": "round-1/share-gone--desktop", "kind": "http_4xx", "why": "the plan asks for a 404"}
        self.assertEqual(run.grade_ui_reviewer(exp, waived, tag, {"expected": [entry]}), [])
        for why, reply in {"none": {}, "no reason": {"expected": [{**entry, "why": " "}]},
                           "other kind": {"expected": [{**entry, "kind": "step_error"}]},
                           "other shot": {"expected": [{**entry, "shot": "round-1/share--desktop"}]},
                           "longer name": {"expected": [{**entry, "shot": "round-1/old-share-gone--desktop"}]}}.items():
            self.assertEqual(len(run.grade_ui_reviewer(exp, waived, tag, reply)), 1, why)
        twin = all_cases()["share-page-not-found"]["expect"]
        major = {"verdict": "changes_requested",
                 "findings": [{"severity": "major", "issue": f"{tag}round-1/share--desktop (/share.html): HTTP 404"}]}
        self.assertEqual(run.grade_ui_reviewer(twin, major, tag, {"expected": []}), [])
        self.assertEqual(len(run.grade_ui_reviewer(twin, major, tag, {"expected": [{**entry, "shot": "share--desktop"}]})), 1)


    def test_manager_grader_matches_task_ids_exactly(self):
        exp = {"touches": ["api"]}
        add = {"actions": [{"action": "add_task", "task": {"id": "api-share", "owns": ["apps/api/x.go"]}}]}
        self.assertEqual(run.grade_manager(exp, add, {}), ["expected an action on api"])
        self.assertEqual(run.grade_manager(exp, {"actions": [{"action": "retry", "task": "api"}]}, {}), [])
        self.assertEqual(run.grade_manager(exp, {"actions": [{"action": "add_task", "task": {"id": "api"}}]}, {}), [])
        self.assertEqual(run.grade_manager(exp, {"actions": [{"action": "retry", "task": "api-share"}]}, {}),
                         ["expected an action on api"])

    def test_manager_grader_mentions_search_the_actions_not_the_note(self):
        exp = {"any_action": ["retry"], "touches": ["api"], "mentions": ["store.go"]}
        note_only = {"note": "store.go is unowned, so the api retry must widen owns",
                     "actions": [{"action": "retry", "task": "api", "instructions": "try again"}]}
        self.assertEqual(run.grade_manager(exp, note_only, {}), ["no action mentions 'store.go'"])
        fixed = {"note": "widen owns",
                 "actions": [{"action": "retry", "task": "api", "instructions": "you may edit apps/api/store.go"}]}
        self.assertEqual(run.grade_manager(exp, fixed, {}), [])
        self.assertEqual(run.grade_manager({}, fixed, {"errors": ["bad"]}), ["invalid actions: ['bad']"])

    def test_planner_grader_checks_the_assumptions(self):
        case = {"done": "D"}
        plan = {"definition_of_done": "D", "acceptance": ["a"], "tasks": [{"id": "t", "owns": ["x/**"]}],
                "assumptions": ["Links are stored in a new share_links table (not in memory)"]}
        own = lambda f, pat: f.startswith(pat.rstrip("*"))  # noqa: E731
        self.assertEqual(run.grade_planner({"assumption_matches": ["stor"]}, case, plan, [], own), [])
        self.assertEqual(len(run.grade_planner({"no_assumption_matches": ["stor"]}, case, plan, [], own)), 1)
        bare = {**plan, "assumptions": []}
        self.assertEqual(len(run.grade_planner({"assumption_matches": ["stor"]}, case, bare, [], own)), 1)
        self.assertEqual(run.grade_planner({"no_assumption_matches": ["stor"]}, case, bare, [], own), [])
        self.assertIn("definition_of_done not verbatim",
                      run.grade_planner({}, case, {**plan, "definition_of_done": "E"}, [], own))
        self.assertEqual(run.grade_planner({"decisions": []}, case, plan, [], own), [])
        self.assertEqual(run.grade_planner({"decisions": []}, case, {**plan, "decisions": []}, [], own), [])
        chose = {**plan, "decisions": ["Links are stored in memory"]}
        self.assertEqual(len(run.grade_planner({"decisions": []}, case, chose, [], own)), 1)
        self.assertEqual(all_cases()["storage-left-open"]["expect"]["decisions"], [],
                         "the planner's own storage choice must not end up in decisions")
        settled = run._from(all_cases()["storage-settled"])
        self.assertTrue(settled["decisions"] and settled["base_files"], "the twin passes --decision on the same fixture")

    def test_every_case_is_well_formed_and_negatives_are_paired(self):
        ids = all_cases()
        for c in ids.values():
            if c["_role"] == "parent":
                self.assertTrue(1 <= len(c["turns"]) <= 2, c["id"])
                for t in c["turns"]:
                    self.assertIsInstance(t["user"], str)
                    self.assertTrue(set(t.get("expect") or {}) <= run.PARENT_EXPECT, (c["id"], t.get("expect")))
                    exp = t.get("expect") or {}
                    self.assertTrue(set(exp) & run.PARENT_POSITIVE,
                                    f"{c['id']}: a turn needs a check that only passes when it did something")
                    for k, v in exp.items():
                        if isinstance(v, bool):  # files_unchanged, no_in_place_edit
                            continue
                        if k in ("reply_includes", "said_includes"):
                            continue
                        for x in v:
                            for rx in (x if k == "calls_before" else [x]):
                                re.compile(rx)
                    for pair in exp.get("calls_before", []):
                        self.assertEqual(len(pair), 2, (c["id"], pair))
            else:
                self.assertIn("expect", c, c["id"])
            if c["_role"] == "ui_reviewer":
                for s in run._from(c)["shots"]:
                    png = (run.UI_FIXTURES / s["png"]).read_bytes()
                    self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n", s["png"])
                    width = int.from_bytes(png[16:20], "big")
                    self.assertEqual(width, {"desktop": 1440, "mobile": 780}[s["viewport"]], s["png"])
        for c in ids.values():
            if c.get("pair"):
                self.assertIn(c["pair"], ids, f"{c['id']} pairs with a missing case")
            if c.get("from"):
                self.assertTrue((CASES / f"{c['from']}.json").exists(), c["id"])
        self.assertGreaterEqual(len([c for c in ids.values() if c.get("core")]), 10)

    # Prompt builders and the eval role that covers each. The worker prompt has no tag (it is the
    # brief); the worker cases cover it.
    PROMPT_ROLES = {"ORCHESTRATE_REVIEW": "reviewer", "ORCHESTRATE_MANAGE": "manager",
                    "ORCHESTRATE_PLAN": "planner", "ORCHESTRATE_ADVISE_": "advisor",  # ADVISE_{stage}
                    "FAILURE_CLASSIFIER": "classifier", "ORCHESTRATE_UI_REVIEW": "ui_reviewer",
                    "ORCHESTRATE_BRIEF": "briefer"}
    EXEMPT = {"ORCHESTRATE_MERGE": "TODO: no eval cases yet",
              "ORCHESTRATE_CLEANUP": "TODO: no eval cases yet",
              "ORCHESTRATE_LESSONS": "TODO: no eval cases yet",
              "ORCHESTRATE_REVIEW_AREA": "TODO: no eval cases yet"}
    # The opening line of a prompt string: the tag, at most one {placeholder}, then a newline.
    # Env var names (ORCHESTRATE_DIR, f"ORCHESTRATE_MODEL_{name}") never end in a newline.
    TAG = re.compile(r'(?:f?"""|f?")(ORCHESTRATE_[A-Z_]+|FAILURE_CLASSIFIER)(?:\{[^}"\n]*\})?(?:\\n|\n)')

    def test_every_prompt_builder_has_eval_cases(self):
        """README: every change to a prompt builder ships with eval cases. A new prompt tag fails
        here until it is mapped to an eval role with cases, or exempted with a reason."""
        found = set()
        for f in sorted((ROOT / "skills" / "orchestrate" / "scripts").glob("*.py")):
            found |= set(self.TAG.findall(f.read_text()))
        self.assertIn("ORCHESTRATE_MANAGE", found, "the tag scan found nothing: has the prompt style changed?")
        unknown = found - set(self.PROMPT_ROLES) - set(self.EXEMPT)
        self.assertFalse(unknown, f"prompt tags with no eval role and no exemption: {sorted(unknown)}")
        stale = set(self.EXEMPT) - found
        self.assertFalse(stale, f"exempt tags no longer in the scripts: {sorted(stale)}")
        self.assertFalse(set(self.PROMPT_ROLES) - found, "mapped tags no longer in the scripts")
        for tag, role in self.PROMPT_ROLES.items():
            self.assertIn(role, run.RUNNERS, tag)
            self.assertTrue(list((CASES / role).glob("*.json")), f"{tag}: no cases in evals/cases/{role}")


if __name__ == "__main__":
    unittest.main()
