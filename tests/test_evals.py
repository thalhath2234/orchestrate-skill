#!/usr/bin/env python3
"""The eval runner's mechanics (grading, consistency, scratch state) with a fake `claude`.

The evals themselves call the real model: `python3 evals/run.py --core`.
Run from the repo root: python3 tests/test_evals.py -v
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE = textwrap.dedent(r'''
    #!/usr/bin/env python3
    import json, sys
    a = sys.argv[1:]
    prompt = a[a.index("-p") + 1]
    if prompt.startswith("FAILURE_CLASSIFIER"):
        r = {"cause": "capability", "evidence": "fake", "agents_md": None}
    elif prompt.startswith("ORCHESTRATE_MANAGE"):
        r = {"note": "nothing to do", "actions": [{"action": "none"}]}
    elif prompt.startswith("ORCHESTRATE_REVIEW"):
        block = prompt.split("Job acceptance criteria:\n", 1)[1].split("\n\n", 1)[0]
        r = {"verdict": "pass", "summary": "fine", "findings": [],
             "acceptance": [{"criterion": l[2:], "met": True, "evidence": "x_test.go:1"}
                            for l in block.splitlines() if l.startswith("- ")]}
    else:
        r = {}
    print(json.dumps({"result": json.dumps(r), "structured_output": r, "is_error": False,
                      "total_cost_usd": 0.02, "session_id": "s", "num_turns": 1}))
''').lstrip()


class EvalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evals-test-"))
        fake = self.tmp / "claude"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        self.env = {**os.environ, "ROUTER_CLAUDE_BIN": str(fake), "ORCHESTRATE_API_BACKOFF_S": "0"}

    def run_evals(self, *args):
        out = self.tmp / "out.json"
        p = subprocess.run([sys.executable, str(ROOT / "evals" / "run.py"), *args, "--json", str(out)],
                           env=self.env, capture_output=True, text=True, timeout=300)
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

    def test_min_pass_gates(self):
        p, _ = self.run_evals("--role", "classifier", "--case", "never-ran", "--min-pass", "0.9")
        self.assertEqual(p.returncode, 1)

    def test_every_case_is_well_formed_and_negatives_are_paired(self):
        ids = {}
        for f in sorted((ROOT / "evals" / "cases").glob("*/*.json")):
            c = json.loads(f.read_text())
            self.assertIn("expect", c, f)
            ids[c["id"]] = c
        for c in ids.values():
            if c.get("pair"):
                self.assertIn(c["pair"], ids, f"{c['id']} pairs with a missing case")
        self.assertGreaterEqual(len([c for c in ids.values() if c.get("core")]), 10)


if __name__ == "__main__":
    unittest.main()
